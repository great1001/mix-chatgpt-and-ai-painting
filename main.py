import collections
import sys
import time
import re

import websocket
import threading
from revChatGPT.V1 import Chatbot

import config
import global_var
from operations import *
from utils import *
from ws_wrapper import *

chatbot = None


def image_message_handler_thread():
    while True:
        if len(global_var.image_gen_messages) > 0:
            message = global_var.image_gen_messages.pop(0)
            gen_message, sender, group_id, show_prompt = message
            if is_not_remote_machine():
                if is_group_online(group_id):
                    try:
                        image, seed, prompt = gen_image(sender, gen_message, group_id)
                        if show_prompt:
                            at_user_in_group(sender, sender,
                                             f"prompt={prompt}\nseed={seed}[CQ:image,file=base64://{image}]",
                                             group_id)
                        else:
                            at_user_in_group(sender, sender, f"seed={seed}[CQ:image,file=base64://{image}]", group_id)
                    except Exception as e:
                        send_err_to_group(sender, e, group_id)
            else:
                if not is_group_online(group_id):
                    at_user_in_group(sender, sender, "该群聊响应未上线", group_id)
                elif gpu_disconnect_notify and not global_var.is_gpu_connected:
                    at_user_in_group(sender, sender, gpu_disconnected_msg, group_id)
        time.sleep(0.3)


def get_chat_pair(group_id, sender):
    history_id = get_sender_key_in_group(group_id, sender)
    if history_id not in global_var.chat_history:
        global_var.chat_history[history_id] = collections.deque(maxlen=context_length)
        return ''

    if len(global_var.chat_history[history_id]) == 0:
        return ''
    else:
        if not global_var.use_chatgpt:
            chat_pair = ''
            for chat in global_var.chat_history[history_id]:
                chat_pair += 'Human:' + chat['question'] + '\nAI:' + chat['answer'] + '\n'
            return chat_pair
        else:
            chat_pair = []
            for chat in global_var.chat_history[history_id]:
                chat_pair.append({"role": "user", "content": chat['question']})
                chat_pair.append({"role": "assistant", "content": chat['answer']})
            return chat_pair


def chat_handler_thread(group_id, question, sender):
    global chatbot

    if not is_group_online(group_id) or (gpu_disconnect_notify and not global_var.is_gpu_connected):
        if is_remote_machine():
            at_user_in_group(sender, sender, "喵喵不在线哦~", group_id)
        return

    if sender != master_id and not is_vip(sender):
        if is_remote_machine():
            at_user_in_group(sender, sender, "你不是喵喵的主人哦~", group_id)
        return

    if is_user_banned(sender):
        if is_remote_machine():
            at_user_in_group(sender, sender, "你被拉黑了喵~", group_id)
        return

    if global_var.is_remote_machine:
        return

    answer = ""
    if not global_var.use_chatgpt:
        try:
            chat_prompt = gpt_prompt_base + get_chat_pair(group_id, sender) + 'Human:' + question + '\nAI:'
            completion = openai.Completion.create(engine="text-davinci-003", prompt=chat_prompt, max_tokens=500,
                                                  timeout=api_timeout, stop=['Human:', 'AI:'])
            answer = completion.choices[0].text
        except Exception as e:
            send_err_to_group(sender, e, group_id)
            return
    else:
        try:
            if not billing_chatgpt:
                if not chatbot:
                    chatbot = Chatbot(config={
                        "email": config.email,
                        "password": config.password
                    })
                chatbot.conversation_id = None
                chatbot.parent_id = None
                chat_prompt = gpt_prompt_base + get_chat_pair(group_id, sender) + 'Human:' + question + '\nAI:'
                for data in chatbot.ask(chat_prompt, None, None, api_timeout):
                    answer = data["message"]
            else:
                pair = get_chat_pair(group_id, sender)
                chat_prompt = (pair if pair else [])
                chat_prompt.insert(0, {"role": "system", "content": chatgpt_prompt_base})
                chat_prompt.append({"role": "user", "content": question})
                completion = openai.ChatCompletion.create(model="gpt-3.5-turbo", messages=chat_prompt,
                                                          timeout=api_timeout)
                answer = completion.choices[0].message.content
        except Exception as e:
            send_err_to_group(sender, e, group_id)
            return

    global_var.chat_history[get_sender_key_in_group(group_id, sender)].append({"question": question, "answer": answer})

    pattern = r"\[paint_prompt:\s*(.*?)\]"
    match = re.search(pattern, answer)
    if match:
        at_user_in_group(sender, sender, re.sub(pattern, '', answer), group_id)
        extracted_text = match.group(1)
        global_var.image_gen_messages.append(({"prompt": extracted_text}, sender, group_id, True))
    else:
        at_user_in_group(sender, sender, answer, group_id)


def message_handler(message: str, sender, group_id):
    if sender == bot_id:
        print(f"bot response in {group_id}: {message[:60]}...")
        if gpu_connected_msg in message:
            global_var.is_gpu_connected = True
            global_var.banned_user_id.clear()
        elif gpu_disconnected_msg in message:
            global_var.is_gpu_connected = False
        elif start_gen_tag_msg in message:
            global_var.gpu_connect_confirm_timer.reset()
        return
    print(f"get {message} from {group_id}, sender: {sender}")

    if message.startswith(f'[CQ:at,qq={bot_id}]'):
        message = message.replace(f'[CQ:at,qq={bot_id}]', '')
        message = message.strip()
        if not message.startswith('#'):
            threading.Thread(target=chat_handler_thread, args=(group_id, message, sender)).start()
    elif not message.startswith('#') and get_sender_key_in_group(group_id, sender) in global_var.users_not_need_at:
        threading.Thread(target=chat_handler_thread, args=(group_id, message, sender)).start()

    if message.startswith('#'):
        if is_user_banned(sender):
            if is_remote_machine():
                at_user_in_group(sender, sender, "你在黑名单中, 操作被拒绝", group_id)
            return

        for command, ops in both_operations.items():
            if message.startswith(command):
                ops(sender, message, group_id)
                return

        if is_remote_machine():
            found_cmd = False
            for command, ops in remote_operations.items():
                if message.startswith(command):
                    ops(sender, message, group_id)
                    found_cmd = True
                    break
            if not found_cmd:
                operation_general_response(sender, message, group_id)


def on_message(self, message):
    data = json.loads(message)
    # print(data)
    if "post_type" in data and (data["post_type"] == "message" or data["post_type"] == "message_sent") \
            and "message_type" in data and data["message_type"] == "group" \
            and "group_id" in data:
        message_handler(data["message"], data["sender"]["user_id"], data["group_id"])
    elif "status" in data and data["status"] == "ok" and "data" in data and "message_id" in data["data"]:
        global_var.last_msg_id_of_user[data["echo"]["message_source"]] = data["data"]["message_id"]


def on_error(self, error):
    print("错误:\n" + str(error))


def on_open(self):
    print("连接成功")
    if gpu_connect_notify and is_not_remote_machine():
        for group in working_groups:
            send_message_to_group(bot_id, gpu_connected_msg, group)


if __name__ == "__main__":
    global_var.init()

    if len(sys.argv) > 1:
        global_var.is_remote_machine = True
    else:
        global_var.is_remote_machine = False
        global_var.is_gpu_connected = True
        import openai

        openai.api_key = api_key

    websocket.enableTrace(False)
    global_var.ws = websocket.WebSocketApp(ws_url, on_message=on_message, on_error=on_error, on_open=on_open)

    threading.Thread(target=image_message_handler_thread).start()
    global_var.ws.run_forever(reconnect=3)
