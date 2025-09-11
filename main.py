import os
import json
import random
import asyncio
import threading
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.errors import UserPrivacyRestrictedError, FloodWaitError
from tkinter import *
from tkinter import ttk, scrolledtext, messagebox

load_dotenv()


# Config / defaults

CUSTOM_TEXT_PATH = "custom/custom_text.txt"
ADDUSERLIST = "process/input/userBase.json"
OUTPUT_DIR = "process/output"
CLIENTS_CONFIG = "process/config/clients.json"  # optional file listing multiple clients
GROUP_LINK = "https://t.me/yourgroup"  # substitute with real link or user input in UI

FALLBACK_API_ID = os.getenv("api_id")
FALLBACK_API_HASH = os.getenv("api_hash")


# Helpers for UI thread-safe updates

def ui_update_label(label: Label, text: str):
    if not label:
        return
    label.after(0, lambda: label.config(text=text))

def ui_append_text(widget: Text, text: str):
    if not widget:
        return
    def _append():
        widget.insert(END, text + "\n")
        widget.see(END)
    widget.after(0, _append)


# Asyncio loop em thread separada

loop = asyncio.new_event_loop()

def start_async_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()

threading.Thread(target=start_async_loop, daemon=True).start()

def schedule_coro(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop)


# Load clients config

def load_clients_config():
    if os.path.exists(CLIENTS_CONFIG):
        with open(CLIENTS_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            if isinstance(cfg, list) and cfg:
                return cfg
            else:
                raise ValueError("clients.json must contain a non-empty list of clients.")
    if FALLBACK_API_ID and FALLBACK_API_HASH:
        return [{"session": "principal", "api_id": int(FALLBACK_API_ID), "api_hash": FALLBACK_API_HASH}]
    raise RuntimeError("No client config found and no API_ID/API_HASH in .env")


# Initialize Telethon clients

clients = []  # list of (client_obj, session_name) tuples

async def start_all_clients():
    cfgs = load_clients_config()
    global clients
    clients = []
    for c in cfgs:
        session = c.get("session") or c.get("session_name") or "session"
        api_id = int(c["api_id"])
        api_hash = c["api_hash"]
        client = TelegramClient(session, api_id, api_hash)
        await client.start()
        who = await client.get_me()
        print(f"[+] Client started: {session} ({who.username} | {who.id})")
        clients.append((client, session))


# Schedule coroutines on clients

def schedule_on_client(client_obj, coro):
    schedule_coro(coro)


# Fetch user IDs

async def fetch_user_ids_async(client_obj, status_label, user_list):
    total = len(user_list)
    for idx, user in enumerate(user_list, start=1):
        username = user.get("username")
        if not username:
            user["id"] = None
            user["status"] = "failed"
            user["message_send"] = "empty username"
            ui_update_label(status_label, f"[{idx}/{total}] empty username -> skipped")
            continue
        try:
            entity = await client_obj.get_entity(username)
            user["id"] = entity.id
            ui_update_label(status_label, f"[{idx}/{total}] found {username} -> id {entity.id}")
        except Exception as e:
            user["id"] = None
            user["status"] = "failed"
            user["message_send"] = str(e)
            ui_update_label(status_label, f"[{idx}/{total}] failed {username}: {e}")
        await asyncio.sleep(0.2)
    ui_update_label(status_label, "Convert finished!")
    return user_list

async def convert_async(status_label, input_path="process/input/userBase.txt", output_path=ADDUSERLIST, client_for_lookup=None):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    user_list = []
    if not os.path.exists(input_path):
        ui_update_label(status_label, f"Input file not found: {input_path}")
        return
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            username = line.strip()
            if username:
                user_list.append({"username": username, "id": None, "status": "pending", "message_send": ""})
    ui_update_label(status_label, f"[0/{len(user_list)}] base JSON created, resolving IDs...")
    if client_for_lookup is None:
        if not clients:
            ui_update_label(status_label, "No clients available to resolve usernames.")
            return
        client_for_lookup = clients[0][0]
    user_list = await fetch_user_ids_async(client_for_lookup, status_label, user_list)
    with open(output_path, "w", encoding="utf-8") as fw:
        json.dump(user_list, fw, ensure_ascii=False, indent=4)
    ui_update_label(status_label, f"Saved {len(user_list)} users to {output_path}")


# Add users

async def add_users_worker(client_obj, users_subset, status_label, group_entity):
    added, failed, pending = [], [], []
    total = len(users_subset)
    for i, user in enumerate(users_subset, start=1):
        username = user.get("username")
        uid = user.get("id")
        if user.get("status") != "pending":
            ui_update_label(status_label, f"[worker] Skipping {username} (status {user.get('status')})")
            continue
        if not uid:
            user["status"] = "failed"
            user["message_send"] = "User ID not found"
            failed.append(user)
            ui_update_label(status_label, f"[{i}/{total}] {username} -> no id")
            continue
        try:
            await client_obj(InviteToChannelRequest(channel=group_entity, users=[uid]))
            user["status"] = "added"
            added.append(user)
            ui_update_label(status_label, f"[{i}/{total}] [+] Added {username}")
        except UserPrivacyRestrictedError:
            user["status"] = "pending"
            user["message_send"] = "User privacy prevents adding"
            pending.append(user)
            ui_update_label(status_label, f"[{i}/{total}] [!] Privacy - {username}")
        except FloodWaitError as e:
            wait_time = getattr(e, "seconds", 30)
            ui_update_label(status_label, f"[!] FloodWait on account {client_obj.session.filename}: waiting {wait_time}s")
            await asyncio.sleep(wait_time + 3)
            try:
                await asyncio.sleep(1)
                await client_obj(InviteToChannelRequest(channel=group_entity, users=[uid]))
                user["status"] = "added"
                added.append(user)
                ui_update_label(status_label, f"[retry] Added {username}")
            except Exception as e2:
                user["status"] = "failed"
                user["message_send"] = str(e2)
                failed.append(user)
                ui_update_label(status_label, f"[-] Failed {username}: {e2}")
        except Exception as e:
            user["status"] = "failed"
            user["message_send"] = str(e)
            failed.append(user)
            ui_update_label(status_label, f"[-] Failed {username}: {e}")
        await asyncio.sleep(random.uniform(4.0, 10.0))
    return {"added": added, "failed": failed, "pending": pending}

def partition_users_evenly(user_list, n_parts):
    parts = [[] for _ in range(n_parts)]
    for idx, user in enumerate(user_list):
        parts[idx % n_parts].append(user)
    return parts

async def add_users_coordinator(status_label, add_list_path=ADDUSERLIST, group_link=GROUP_LINK):
    if not clients:
        ui_update_label(status_label, "No clients available to add users.")
        return
    if not os.path.exists(add_list_path):
        ui_update_label(status_label, f"Add list not found: {add_list_path}")
        return

    with open(add_list_path, "r", encoding="utf-8") as f:
        user_list = json.load(f)

    master_client = clients[0][0]
    try:
        group_entity = await master_client.get_entity(group_link)
    except Exception as e:
        ui_update_label(status_label, f"Could not resolve group {group_link}: {e}")
        return

    ui_update_label(status_label, f"Starting adding {len(user_list)} users using {len(clients)} accounts...")
    parts = partition_users_evenly(user_list, len(clients))
    worker_tasks = []
    for (client_obj, session_name), users_subset in zip(clients, parts):
        if not users_subset:
            continue
        task = asyncio.create_task(add_users_worker(client_obj, users_subset, status_label, group_entity))
        worker_tasks.append(task)

    results = await asyncio.gather(*worker_tasks, return_exceptions=True)
    agg_added, agg_failed, agg_pending = [], [], []
    for r in results:
        if isinstance(r, Exception):
            ui_update_label(status_label, f"Worker error: {r}")
            continue
        agg_added.extend(r.get("added", []))
        agg_failed.extend(r.get("failed", []))
        agg_pending.extend(r.get("pending", []))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "user_added.json"), "w", encoding="utf-8") as f:
        json.dump(agg_added, f, ensure_ascii=False, indent=4)
    with open(os.path.join(OUTPUT_DIR, "user_failed.json"), "w", encoding="utf-8") as f:
        json.dump(agg_failed, f, ensure_ascii=False, indent=4)
    with open(os.path.join(OUTPUT_DIR, "user_pending.json"), "w", encoding="utf-8") as f:
        json.dump(agg_pending, f, ensure_ascii=False, indent=4)

    ui_update_label(status_label, f"Add process finished! added={len(agg_added)} failed={len(agg_failed)} pending={len(agg_pending)}")


# Send messages

async def send_messages_worker(client_obj, users, messages_list, status_label, group_link_text):
    sent, failed = [], []
    total = len(users)
    for i, user in enumerate(users, start=1):
        uid = user.get("id")
        username = user.get("username")
        if not uid:
            ui_update_label(status_label, f"[send] skip {username} (no id)")
            failed.append(user)
            continue
        msg = random.choice(messages_list) if messages_list else group_link_text or ""
        if "{link}" in msg:
            msg = msg.replace("{link}", group_link_text or "")
        try:
            await client_obj.send_message(uid, msg)
            user["message_send"] = "sent"
            sent.append(user)
            ui_update_label(status_label, f"[{i}/{total}] Sent to {username}")
        except FloodWaitError as e:
            wait_time = getattr(e, "seconds", 30)
            ui_update_label(status_label, f"[!] FloodWait while sending: waiting {wait_time}s")
            await asyncio.sleep(wait_time + 3)
            try:
                await client_obj.send_message(uid, msg)
                user["message_send"] = "sent"
                sent.append(user)
                ui_update_label(status_label, f"[retry] Sent to {username}")
            except Exception as e2:
                user["message_send"] = f"failed: {e2}"
                failed.append(user)
                ui_update_label(status_label, f"[-] Failed send {username}: {e2}")
        except Exception as e:
            user["message_send"] = f"failed: {e}"
            failed.append(user)
            ui_update_label(status_label, f"[-] Failed send {username}: {e}")
        await asyncio.sleep(random.uniform(2.0, 6.0))
    return {"sent": sent, "failed": failed}

async def send_custom_messages_coordinator(status_label, messages_list, link_text=""):
    if not clients:
        ui_update_label(status_label, "No clients available to send messages.")
        return
    pending_path = os.path.join(OUTPUT_DIR, "user_pending.json")
    failed_path = os.path.join(OUTPUT_DIR, "user_failed.json")
    users_all = []
    if os.path.exists(failed_path):
        with open(failed_path, "r", encoding="utf-8") as f:
            users_all.extend(json.load(f))
    if os.path.exists(pending_path):
        with open(pending_path, "r", encoding="utf-8") as f:
            users_all.extend(json.load(f))
    if not users_all:
        ui_update_label(status_label, "No users to message (failed/pending files empty).")
        return
    ui_update_label(status_label, f"Sending messages to {len(users_all)} users with {len(clients)} accounts...")
    parts = partition_users_evenly(users_all, len(clients))
    tasks = []
    for (client_obj, _session), subset in zip(clients, parts):
        if not subset:
            continue
        tasks.append(asyncio.create_task(send_messages_worker(client_obj, subset, messages_list, status_label, link_text)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    sent_total, failed_total = 0, 0
    for r in results:
        if isinstance(r, Exception):
            ui_update_label(status_label, f"Worker error: {r}")
            continue
        sent_total += len(r.get("sent", []))
        failed_total += len(r.get("failed", []))

    still_failed = [u for u in users_all if u.get("message_send") != "sent"]
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "user_failed.json"), "w", encoding="utf-8") as fw:
        json.dump(still_failed, fw, ensure_ascii=False, indent=4)

    ui_update_label(status_label, f"Send finished! sent={sent_total} still_failed={failed_total}")


# UI wiring

def schedule_convert(status_label, input_path):
    if not clients:
        ui_update_label(status_label, "No clients available to convert (start clients first).")
        return
    ui_update_label(status_label, "Starting convert (building json & resolving ids)...")
    schedule_coro(convert_async(status_label, input_path=input_path, output_path=ADDUSERLIST, client_for_lookup=clients[0][0]))

def schedule_add_users(status_label):
    if not clients:
        ui_update_label(status_label, "No clients available to add users.")
        return
    ui_update_label(status_label, "Scheduling add users...")
    schedule_coro(add_users_coordinator(status_label, add_list_path=ADDUSERLIST, group_link=GROUP_LINK))

def schedule_send_messages(status_label, messages_list, link_text):
    if not clients:
        ui_update_label(status_label, "No clients available to send messages.")
        return
    if not messages_list:
        ui_update_label(status_label, "No messages provided. Please write at least one message.")
        return
    ui_update_label(status_label, "Scheduling send messages...")
    schedule_coro(send_custom_messages_coordinator(status_label, messages_list, link_text))


# Tkinter UI

def build_ui():
    root = Tk()
    root.title("Telegram Multi-Account Adder")
    root.geometry("900x700")
    notebook = ttk.Notebook(root)
    notebook.pack(expand=True, fill="both", padx=8, pady=8)

    # --- Tab 1: Add Users ---
    frame_add = Frame(notebook)
    notebook.add(frame_add, text="Add Users")
    lbl_group = Label(frame_add, text="Group link (or @username):")
    lbl_group.pack(anchor="w", padx=8, pady=(8,0))
    ent_group = Entry(frame_add)
    ent_group.insert(0, GROUP_LINK)
    ent_group.pack(fill="x", padx=8)

    lbl_input = Label(frame_add, text="Input file (one username per line):")
    lbl_input.pack(anchor="w", padx=8, pady=(8,0))
    ent_input = Entry(frame_add)
    ent_input.insert(0, "process/input/userBase.txt")
    ent_input.pack(fill="x", padx=8)

    lbl_status_add = Label(frame_add, text="Ready", font=("Arial", 12))
    lbl_status_add.pack(pady=6)
    log_add = scrolledtext.ScrolledText(frame_add, height=12)
    log_add.pack(fill="both", padx=8, pady=6, expand=True)

    def on_convert_clicked():
        path = ent_input.get().strip()
        if path:
            ui_update_label(lbl_status_add, "Starting convert...")
            schedule_convert(lbl_status_add, input_path=path)
            ui_append_text(log_add, f"[+] Scheduled convert for {path}")
        else:
            ui_update_label(lbl_status_add, "Input path empty.")

    def on_add_clicked():
        glink = ent_group.get().strip()
        if glink:
            global GROUP_LINK
            GROUP_LINK = glink
            ui_update_label(lbl_status_add, f"Group set to {glink}. Scheduling add...")
            ui_append_text(log_add, f"[+] Scheduled add to {glink}")
            schedule_add_users(lbl_status_add)
        else:
            ui_update_label(lbl_status_add, "Group link empty.")

    btn_convert = Button(frame_add, text="Convert TXT -> JSON (resolve IDs)", command=on_convert_clicked)
    btn_convert.pack(pady=6, padx=8, anchor="w")
    btn_add = Button(frame_add, text="Start Adding Users", command=on_add_clicked)
    btn_add.pack(pady=6, padx=8, anchor="w")

    # --- Tab 2: Send Messages ---
    frame_send = Frame(notebook)
    notebook.add(frame_send, text="Send Messages")
    lbl_messages = Label(frame_send, text="Messages (one per line, can use {link}):")
    lbl_messages.pack(anchor="w", padx=8, pady=(8,0))
    txt_messages = scrolledtext.ScrolledText(frame_send, height=12)
    txt_messages.pack(fill="both", padx=8, pady=6, expand=True)

    lbl_status_send = Label(frame_send, text="Ready", font=("Arial", 12))
    lbl_status_send.pack(pady=6)

    def on_send_clicked():
        lines = txt_messages.get("1.0", END).strip().splitlines()
        lines = [l for l in lines if l.strip()]
        if not lines:
            ui_update_label(lbl_status_send, "No messages to send.")
            return
        schedule_send_messages(lbl_status_send, lines, GROUP_LINK)

    btn_send = Button(frame_send, text="Send Messages", command=on_send_clicked)
    btn_send.pack(pady=6, padx=8, anchor="w")

    return root


# Main

if __name__ == "__main__":
    # Start clients at launch
    schedule_coro(start_all_clients())
    ui_root = build_ui()
    ui_root.mainloop()
