import os
import json
import random
import asyncio
import threading
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.errors import UserPrivacyRestrictedError, FloodWaitError, UserAlreadyParticipantError
from tkinter import *
from tkinter import ttk, scrolledtext, messagebox

load_dotenv()


# Config / defaults

CUSTOM_TEXT_PATH = "custom/custom_text.txt"
ADDUSERLIST = "process/input/pending.json"
PENDING_JSON = "process/input/pending.json"
OUTPUT_DIR = "process/output"
CLIENTS_CONFIG = "process/config/clients.json"  # optional file listing multiple clients
GROUP_LINK = "https://t.me/chatterspalace"  # substitute with real link or user input in UI

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
selected_client = None

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
    return clients

def select_client_dialog(clients_list):
    root = Tk()
    root.title("Select Client")
    root.geometry("300x200")
    root.focus_force()
    root.grab_set()
    label = Label(root, text="Select a client to use:")
    label.pack(pady=10)
    client_names = [session for _, session in clients_list]
    combo = ttk.Combobox(root, values=client_names, state="readonly")
    combo.pack(pady=10)
    selected = [None]
    def on_ok():
        selected[0] = combo.get()
        root.destroy()
    btn = Button(root, text="OK", command=on_ok)
    btn.pack(pady=10)
    root.mainloop()
    return selected[0]


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

import math

async def add_users_worker(client_obj, users_subset, status_label, group_entity, max_retries=3):
    added, failed, pending = [], [], []
    total = len(users_subset)
    for i, user in enumerate(users_subset, start=1):
        username = user.get("username")
        uid = user.get("id")
        retries = 0
        # Process all users with an ID regardless of status
        if not uid:
            user["status"] = "failed"
            user["message_send"] = "User ID not found"
            failed.append(user)
            ui_update_label(status_label, f"[{i}/{total}] {username} -> no id")
            continue

        while retries <= max_retries:
            try:
                await client_obj(InviteToChannelRequest(channel=group_entity, users=[uid]))
                user["status"] = "added"
                added.append(user)
                ui_update_label(status_label, f"[{i}/{total}] [+] Added {username}")
                break
            except UserPrivacyRestrictedError:
                user["status"] = "pending"
                user["message_send"] = "User privacy prevents adding"
                pending.append(user)
                ui_update_label(status_label, f"[{i}/{total}] [!] Privacy - {username}")
                break
            except FloodWaitError as e:
                wait_time = getattr(e, "seconds", 30)
                backoff = min(wait_time * (2 ** retries), 600)  # exponential backoff capped at 10 minutes
                ui_update_label(status_label, f"[!] FloodWait on account {client_obj.session.filename}: waiting {backoff}s (retry {retries+1})")
                await asyncio.sleep(backoff)
                retries += 1
            except Exception as e:
                # Check for "Could not find the input entity" error and try to re-fetch entity once
                if "Could not find the input entity" in str(e) and retries == 0:
                    try:
                        entity = await client_obj.get_entity(username)
                        user["id"] = entity.id
                        uid = entity.id
                        ui_update_label(status_label, f"[{i}/{total}] Refetched entity for {username} -> id {entity.id}")
                        retries += 1
                        continue
                    except Exception as e2:
                        user["status"] = "failed"
                        user["message_send"] = f"Entity refetch failed: {e2}"
                        failed.append(user)
                        ui_update_label(status_label, f"[-] Failed {username}: {e2}")
                        break
                else:
                    user["status"] = "failed"
                    user["message_send"] = str(e)
                    failed.append(user)
                    ui_update_label(status_label, f"[-] Failed {username}: {e}")
                    break
        else:
            # Exceeded max retries
            user["status"] = "failed"
            user["message_send"] = "Max retries exceeded due to FloodWaitError"
            failed.append(user)
            ui_update_label(status_label, f"[-] Failed {username}: Max retries exceeded")

        await asyncio.sleep(random.uniform(6.0, 12.0))  # increased delay to reduce rate limit hits

    return {"added": added, "failed": failed, "pending": pending}

def partition_users_evenly(user_list, n_parts):
    parts = [[] for _ in range(n_parts)]
    for idx, user in enumerate(user_list):
        parts[idx % n_parts].append(user)
    return parts

def clean_username(raw_username: str) -> str:
    # Remove spaces, @, URLs, and trailing parts after slash
    username = raw_username.strip()
    if username.startswith("https://t.me/"):
        username = username[len("https://t.me/"):]
    username = username.split()[0]  # take first part if spaces
    username = username.split("/")[0]  # take first part if slash
    username = username.lstrip("@").strip()
    return username

async def add_users_coordinator(status_label, input_path="process/input/userBase.txt", group_link=GROUP_LINK, selected_client=None):
    if not clients:
        ui_update_label(status_label, "No clients available to add users.")
        return
    if not os.path.exists(input_path):
        ui_update_label(status_label, f"User base file not found: {input_path}")
        return

    with open(input_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Clean usernames from lines
    usernames = [clean_username(line) for line in lines if line.strip()]

    # Find the selected client
    selected_client_obj = None
    if selected_client:
        for client_obj, session in clients:
            if session == selected_client:
                selected_client_obj = client_obj
                break
        if not selected_client_obj:
            ui_update_label(status_label, f"Selected client '{selected_client}' not found.")
            return
    else:
        selected_client_obj = clients[0][0] if clients else None
        if not selected_client_obj:
            ui_update_label(status_label, "No clients available.")
            return

    try:
        group_entity = await selected_client_obj.get_entity(group_link)
    except Exception as e:
        ui_update_label(status_label, f"Could not resolve group {group_link}: {e}")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    added_path = os.path.join(OUTPUT_DIR, "added.json")
    failed_path = os.path.join(OUTPUT_DIR, "failed.json")

    # Initialize output files if not exist
    if not os.path.exists(added_path):
        with open(added_path, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=4)
    if not os.path.exists(failed_path):
        with open(failed_path, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=4)

    added_all = []
    failed_all = []

    for i, username in enumerate(usernames, start=1):
        uid = None
        try:
            entity = await selected_client_obj.get_entity(username)
            uid = entity.id
            ui_update_label(status_label, f"[{i}/{len(usernames)}] Found ID for {username}: {uid}")
        except Exception as e:
            ui_update_label(status_label, f"[{i}/{len(usernames)}] Failed to get ID for {username}: {e}")

        if not uid:
            ui_update_label(status_label, f"[{i}/{len(usernames)}] No ID for {username}, skipping add")
            # Remove line from file
            lines = [line for line in lines if clean_username(line) != username]
            with open(input_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            continue

        retries = 0
        added = False
        while retries <= 3:
            try:
                await selected_client_obj(InviteToChannelRequest(channel=group_entity, users=[uid]))
                ui_update_label(status_label, f"[{i}/{len(usernames)}] [+] Added {username}")
                added_all.append({"username": username, "id": uid})
                added = True
                break
            except UserAlreadyParticipantError:
                ui_update_label(status_label, f"[{i}/{len(usernames)}] [!] {username} is already in the group")
                added_all.append({"username": username, "id": uid, "status": "already in group"})
                added = True
                break
            except UserPrivacyRestrictedError:
                ui_update_label(status_label, f"[{i}/{len(usernames)}] [!] Privacy prevents adding {username}")
                failed_all.append({"username": username, "id": uid})
                break
            except FloodWaitError as e:
                wait_time = getattr(e, "seconds", 30)
                ui_update_label(status_label, f"[!] FloodWait: waiting {wait_time}s (retry {retries+1})")
                await asyncio.sleep(wait_time)
                retries += 1
            except Exception as e:
                ui_update_label(status_label, f"[-] Failed to add {username}: {e}")
                failed_all.append({"username": username, "id": uid})
                break
        else:
            ui_update_label(status_label, f"[-] Failed {username}: Max retries exceeded")
            failed_all.append({"username": username, "id": uid})

        # Remove line from file after processing
        lines = [line for line in lines if clean_username(line) != username]
        with open(input_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        # Save added and failed users
        if added:
            with open(added_path, "r", encoding="utf-8") as f:
                existing_added = json.load(f)
            existing_added.append({"username": username, "id": uid})
            with open(added_path, "w", encoding="utf-8") as f:
                json.dump(existing_added, f, ensure_ascii=False, indent=4)
        else:
            with open(failed_path, "r", encoding="utf-8") as f:
                existing_failed = json.load(f)
            existing_failed.append({"username": username, "id": uid})
            with open(failed_path, "w", encoding="utf-8") as f:
                json.dump(existing_failed, f, ensure_ascii=False, indent=4)

        await asyncio.sleep(random.uniform(1.0, 3.0))  # short delay to respect floodwait

    ui_update_label(status_label, f"Add process finished! Added: {len(added_all)}, Failed: {len(failed_all)}")


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
    pending_path = PENDING_JSON
    failed_path = os.path.join(OUTPUT_DIR, "failed.json")
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
    with open(failed_path, "w", encoding="utf-8") as fw:
        json.dump(still_failed, fw, ensure_ascii=False, indent=4)

    ui_update_label(status_label, f"Send finished! sent={sent_total} still_failed={failed_total}")


# UI wiring

def schedule_convert(status_label, input_path):
    if not clients:
        ui_update_label(status_label, "No clients available to convert (start clients first).")
        return
    ui_update_label(status_label, "Starting convert (building json & resolving ids)...")
    schedule_coro(convert_async(status_label, input_path=input_path, output_path=ADDUSERLIST, client_for_lookup=clients[0][0]))

def schedule_add_users(status_label, selected_client=None):
    if not clients:
        ui_update_label(status_label, "No clients available to add users.")
        return
    ui_update_label(status_label, "Scheduling add users...")
    schedule_coro(add_users_coordinator(status_label, input_path="process/input/userBase.txt", group_link=GROUP_LINK, selected_client=selected_client))

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

    lbl_status_add = Label(frame_add, text="Ready", font=("Arial", 12))
    lbl_status_add.pack(pady=6)
    log_add = scrolledtext.ScrolledText(frame_add, height=12)
    log_add.pack(fill="both", padx=8, pady=6, expand=True)

    def on_add_clicked():
        glink = ent_group.get().strip()
        if glink:
            global GROUP_LINK, selected_client
            GROUP_LINK = glink
            ui_update_label(lbl_status_add, f"Group set to {glink}. Scheduling add...")
            ui_append_text(log_add, f"[+] Scheduled add to {glink} with client: {selected_client}")
            schedule_add_users(lbl_status_add, selected_client=selected_client)
        else:
            ui_update_label(lbl_status_add, "Group link empty.")

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
    import time
    time.sleep(5)  # Wait for clients to start
    if clients:
        selected_client = select_client_dialog(clients)
    ui_root = build_ui()
    ui_root.mainloop()
