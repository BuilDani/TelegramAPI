import os
import json
import random
import asyncio
import threading
import tempfile
import re
import time
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.errors import UserPrivacyRestrictedError, FloodWaitError
from tkinter import *
from tkinter import ttk, scrolledtext, messagebox
import sqlite3
import shutil
from pathlib import Path
import math

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


# File I/O helpers to avoid race conditions

FILE_IO_LOCK = threading.Lock()

def safe_write_json(path, data):
    """
    Write JSON atomically using a temp file + os.replace protected by FILE_IO_LOCK.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="tmp_", suffix=".json", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fw:
            json.dump(data, fw, ensure_ascii=False, indent=4)
            fw.flush()
            os.fsync(fw.fileno())
        with FILE_IO_LOCK:
            os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def update_user_in_json(path, username, update_fn):
    """
    Atomic read-modify-write for a single user entry (by username) in a list JSON file.
    update_fn receives the existing dict (or {'username': username}) and must return the updated dict.
    Guard: do nothing if username is falsy/invalid to avoid creating empty/null entries.
    """
    # guard invalid usernames to avoid creating empty records
    if not username or not isinstance(username, str) or not username.strip():
        return

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with FILE_IO_LOCK:
        data = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fr:
                    data = json.load(fr)
                    if not isinstance(data, list):
                        data = []
            except Exception:
                data = []
        found = False
        for idx, u in enumerate(data):
            if u.get("username") == username:
                new_u = update_fn(u.copy())
                if new_u is None:
                    new_u = u
                data[idx] = new_u
                found = True
                break
        if not found:
            new_u = update_fn({"username": username})
            if new_u is None:
                new_u = {"username": username}
            data.append(new_u)
        # atomic write
        fd, tmp_path = tempfile.mkstemp(prefix="tmp_", suffix=".json", dir=os.path.dirname(path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fw:
                json.dump(data, fw, ensure_ascii=False, indent=4)
                fw.flush()
                os.fsync(fw.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


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
    for idx, c in enumerate(cfgs):
        # ensure per-client unique session filename to avoid session DB conflicts
        raw_session = c.get("session") or c.get("session_name") or f"session_{idx}"
        # sanitize session name to safe filename
        session = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in raw_session) or f"session_{idx}"
        api_id = int(c["api_id"])
        api_hash = c["api_hash"]
        client = TelegramClient(session, api_id, api_hash)

        # try start with retry/backoff on sqlite "database is locked" or other transient errors
        max_retries = 6
        backoff_base = 1.5
        started = False
        for attempt in range(1, max_retries + 1):
            try:
                await client.start()
                who = await client.get_me()
                print(f"[+] Client started: {session} ({who.username} | {who.id})")
                clients.append((client, session))
                # small pause between starting clients to reduce DB contention
                await asyncio.sleep(0.5)
                started = True
                break
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "database is locked" in msg or "database is busy" in msg:
                    wait = int(backoff_base ** attempt)
                    print(f"[WARN] sqlite locked when starting {session}, retry {attempt}/{max_retries}, sleeping {wait}s")
                    await asyncio.sleep(wait)
                    continue
                else:
                    print(f"[ERROR] OperationalError starting {session}: {e}")
                    # don't raise - skip this client and continue with others
                    break
            except Exception as e:
                # retry on transient errors a few times, otherwise log and continue to next client
                if attempt < max_retries:
                    wait = int(backoff_base ** attempt)
                    print(f"[WARN] start client {session} failed (attempt {attempt}), retrying in {wait}s: {e}")
                    await asyncio.sleep(wait)
                    continue
                print(f"[ERROR] Failed to start client {session} after {max_retries} attempts: {e}")
                break
        if not started:
            print(f"[WARN] Skipping client {session} after failed start attempts.")
            # ensure client disconnected/cleanup if partially started
            try:
                await client.disconnect()
            except Exception:
                pass
    print(f"[INFO] start_all_clients complete. clients available: {len(clients)}")


# Schedule coroutines on clients

def schedule_on_client(client_obj, coro):
    schedule_coro(coro)


# Fetch user IDs

def parse_floodwait_seconds(msg: str):
    if not msg:
        return None
    m = re.search(r'wait of\s+(\d+)\s+seconds', msg)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def migrate_failed_to_floodwait(path):
    """
    Scan JSON file and convert 'failed' entries that contain "A wait of X seconds"
    into status 'flood_wait' with 'wait_until' (unix ts). Saves atomically.
    """
    if not os.path.exists(path):
        return
    with FILE_IO_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as fr:
                data = json.load(fr)
        except Exception:
            return
        changed = False
        now = int(time.time())
        for u in data:
            if u.get("status") == "failed":
                sec = parse_floodwait_seconds(u.get("message_send", ""))
                if sec and sec > 0:
                    u["status"] = "flood_wait"
                    u["wait_until"] = now + sec
                    # keep message_send for debugging
                    changed = True
        if changed:
            safe_write_json(path, data)

def cleanup_wait_failed_entries(path):
    """
    Remove entries that were previously gravadas com mensagem 'A wait of ... seconds'
    para evitar que essas entradas eternizem bloqueios no processo.
    """
    if not os.path.exists(path):
        return
    with FILE_IO_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as fr:
                data = json.load(fr)
                if not isinstance(data, list):
                    return
        except Exception:
            return
        changed = False
        cleaned = []
        for u in data:
            msg = (u.get("message_send") or "")
            sec = parse_floodwait_seconds(msg)
            if sec and sec > 0:
                # remove this entry so it will be reprocessed (do NOT keep as failed)
                changed = True
                continue
            cleaned.append(u)
        if changed:
            try:
                safe_write_json(path, cleaned)
            except Exception:
                pass

# call migration and cleanup at startup after constants and FILE_IO_LOCK defined
try:
    migrate_failed_to_floodwait(ADDUSERLIST)
    cleanup_wait_failed_entries(ADDUSERLIST)
except Exception as e:
    # evita crash no startup se JSON estiver inválido; log para terminal
    print(f"[WARN] migrate_failed_to_floodwait/cleanup_wait_failed_entries skipped due to error: {e}")

async def fetch_user_ids_async(client_obj, status_label, user_list, output_path=None):
    total = len(user_list)

    # load existing on-disk map once (username -> entry) to avoid re-resolving IDs
    disk_map = {}
    if output_path and os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as fr:
                existing = json.load(fr)
                if isinstance(existing, list):
                    disk_map = {u.get("username"): u for u in existing if u.get("username")}
        except Exception:
            disk_map = {}

    for idx, user in enumerate(user_list, start=1):
        # check stop request
        if STOP_EVENT.is_set():
            ui_update_label(status_label, "Stopped by user (fetch ids).")
            break
        username = user.get("username")
        if not username:
            user["id"] = None
            user["status"] = "failed"
            user["message_send"] = "empty username"
            ui_update_label(status_label, f"[{idx}/{total}] empty username -> skipped")
            if output_path:
                try:
                    update_user_in_json(output_path, username, lambda existing: {**existing, **user})
                except Exception:
                    pass
            continue

        # If disk already has an id for this username, reuse it and skip network lookup
        disk_entry = disk_map.get(username)
        if disk_entry:
            # reuse existing id if present
            if disk_entry.get("id"):
                user["id"] = disk_entry.get("id")
                user.setdefault("status", "pending")
                user.setdefault("message_send", disk_entry.get("message_send", ""))
                ui_update_label(status_label, f"[{idx}/{total}] {username} -> reused id {user['id']} from disk")
                if output_path:
                    try:
                        update_user_in_json(output_path, username, lambda existing: {**existing, **user})
                    except Exception:
                        pass
                await asyncio.sleep(0.05)
                continue
            # if disk has this username but status is pending/failed -> skip entirely (do not resolve again)
            if disk_entry.get("status") in ("pending", "failed"):
                ui_update_label(status_label, f"[{idx}/{total}] {username} in disk with status={disk_entry.get('status')} -> skipped")
                await asyncio.sleep(0.05)
                continue

        # also skip if in-memory user already has id (from earlier processing)
        if user.get("id"):
            ui_update_label(status_label, f"[{idx}/{total}] {username} already has id {user.get('id')} -> skipped")
            if output_path:
                try:
                    update_user_in_json(output_path, username, lambda existing: {**existing, **user})
                except Exception:
                    pass
            await asyncio.sleep(0.05)
            continue

        # perform lookup with FloodWait-aware retry (do NOT persist FloodWait as failed)
        while True:
            try:
                entity = await client_obj.get_entity(username)
                user["id"] = entity.id
                user.setdefault("status", "pending")
                user["message_send"] = ""
                ui_update_label(status_label, f"[{idx}/{total}] found {username} -> id {entity.id}")
                # persist this single-user update
                if output_path:
                    try:
                        update_user_in_json(output_path, username, lambda existing: {**existing, **user})
                    except Exception:
                        pass
                break
            except FloodWaitError as e:
                sec = int(getattr(e, "seconds", 0) or 0)
                if sec <= 0:
                    sec = parse_floodwait_seconds(str(e)) or 30
                ui_update_label(status_label, f"[{idx}/{total}] FloodWait resolving {username}: waiting {sec}s (not persisted)...")
                for remaining in range(sec, 0, -1):
                    if STOP_EVENT.is_set():
                        ui_update_label(status_label, "Stopped by user during wait (fetch ids).")
                        break
                    ui_update_label(status_label, f"[{idx}/{total}] FloodWait resolving {username}: {remaining}s")
                    await asyncio.sleep(1)
                if STOP_EVENT.is_set():
                    break
                continue
            except Exception as e:
                # detect textual "A wait of N seconds" pattern in some errors
                sec = parse_floodwait_seconds(str(e))
                if sec and sec > 0:
                    ui_update_label(status_label, f"[{idx}/{total}] Detected wait-of-{sec}s resolving {username} (not persisted). Waiting...")
                    for remaining in range(sec, 0, -1):
                        ui_update_label(status_label, f"[{idx}/{total}] Waiting {remaining}s for {username} (resolve)...")
                        await asyncio.sleep(1)
                    continue  # retry after wait
                # non-flood error -> mark failed and persist
                user["id"] = None
                user["status"] = "failed"
                user["message_send"] = str(e)
                ui_update_label(status_label, f"[{idx}/{total}] failed {username}: {e}")
                if output_path:
                    try:
                        update_user_in_json(output_path, username, lambda existing: {**existing, **user})
                    except Exception:
                        pass
                break

        await asyncio.sleep(0.2)
    ui_update_label(status_label, "Convert finished!")
    return user_list

async def convert_async(status_label, input_path="process/input/userBase.txt", output_path=ADDUSERLIST, client_for_lookup=None):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    user_list = []
    # load existing on-disk map once (username -> entry) to avoid re-resolving IDs
    disk_map = {}
    if output_path and os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as fr:
                existing = json.load(fr)
                if isinstance(existing, list):
                    disk_map = {u.get("username"): u for u in existing if u.get("username")}
        except Exception:
            disk_map = {}

    if not os.path.exists(input_path):
        ui_update_label(status_label, f"Input file not found: {input_path}")
        return

    seen = set()
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            username = line.strip()
            if not username:
                continue
            if username in seen:
                continue
            seen.add(username)
            # se já existe no JSON com status pending/failed -> NÃO re-adiciona (opção B)
            if username in disk_map and disk_map[username].get("status") in ("pending", "failed"):
                continue
            # se já existe com id, preserve entry para reuse
            if username in disk_map and disk_map[username].get("id"):
                entry = disk_map[username]
                entry.setdefault("status", "pending")
                entry.setdefault("message_send", entry.get("message_send", ""))
                user_list.append(entry)
            else:
                user_list.append({"username": username, "id": None, "status": "pending", "message_send": ""})

    ui_update_label(status_label, f"[0/{len(user_list)}] base JSON created, resolving IDs...")
    # choose client for lookup
    if client_for_lookup is None:
        if not clients:
            ui_update_label(status_label, "No clients available to resolve usernames.")
            return
        client_for_lookup = clients[0][0]
    user_list = await fetch_user_ids_async(client_for_lookup, status_label, user_list, output_path=output_path)

    # MERGE: não sobrescrever disco apenas com user_list parcial — mesclar com o que está em disco
    try:
        disk_entries = []
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as fr:
                disk_entries = json.load(fr) or []
        disk_map = {u.get("username"): u for u in disk_entries if u.get("username")}
        # update disk_map with our in-memory results
        for u in user_list:
            if u.get("username"):
                disk_map[u["username"]] = u
        merged = list(disk_map.values())
        safe_write_json(output_path, merged)
    except Exception:
        pass

    ui_update_label(status_label, f"Saved {len(user_list)} users to {output_path}")


# Add users

# novo: controle de flood por cliente (timestamp unix until)
CLIENT_FLOOD = {}  # session_name -> unix_ts_until

async def add_users_worker(client_obj, users_subset, status_label, group_entity, all_users_ref, add_list_path, session_name=None):
    added, failed, pending = [], [], []
    total = len(users_subset)

    # register this worker so stop() can cancel it
    current_task = asyncio.current_task()
    if current_task not in RUNNING_TASKS:
        RUNNING_TASKS.append(current_task)

    try:
        for i, user in enumerate(users_subset, start=1):
            # check stop request
            if STOP_EVENT.is_set():
                ui_update_label(status_label, "Stopped by user (add users).")
                break

            username = user.get("username")
            uid = user.get("id")
            # skip users that are already in memory with non-pending status (we won't reprocess)
            if user.get("status") not in (None, "pending"):
                ui_update_label(status_label, f"[worker:{session_name}] Skipping {username} (status {user.get('status')})")
                continue
            if not uid:
                user["status"] = "failed"
                user["message_send"] = "User ID not found"
                failed.append(user)
                ui_update_label(status_label, f"[{i}/{total}] {username} -> no id")
                # persist failure
                try:
                    update_user_in_json(add_list_path, username, lambda existing: {**existing, **user})
                except Exception:
                    pass
                continue

            # FloodWait handling with in-memory wait (check stop inside countdown)
            while True:
                if STOP_EVENT.is_set():
                    ui_update_label(status_label, "Stopped by user (during loop).")
                    break
                try:
                    await client_obj(InviteToChannelRequest(channel=group_entity, users=[uid]))
                    user["status"] = "added"
                    added.append(user)
                    ui_update_label(status_label, f"[{i}/{total}] [+] Added {username} by {session_name}")
                    # persist added state
                    try:
                        update_user_in_json(add_list_path, username, lambda existing: {**existing, **user})
                    except Exception:
                        pass
                    break
                except UserPrivacyRestrictedError:
                    user["status"] = "pending"
                    user["message_send"] = "User privacy prevents adding"
                    pending.append(user)
                    ui_update_label(status_label, f"[{i}/{total}] [!] Privacy - {username}")
                    # persist privacy as pending
                    try:
                        update_user_in_json(add_list_path, username, lambda existing: {**existing, **user})
                    except Exception:
                        pass
                    break
                except FloodWaitError as e:
                    sec = int(getattr(e, "seconds", 30) or 30)
                    # mark client flood until timestamp so UI can show it
                    if session_name:
                        CLIENT_FLOOD[session_name] = int(time.time()) + sec
                    # wait in-memory, do not persist flood state
                    ui_update_label(status_label, f"[{i}/{total}] FloodWait on {session_name}: waiting {sec}s for {username} (not persisted)...")
                    for remaining in range(sec, 0, -1):
                        ui_update_label(status_label, f"[{i}/{total}] [{session_name}] FloodWait: {remaining}s")
                        await asyncio.sleep(1)
                    # flood expired: clear marker
                    if session_name and session_name in CLIENT_FLOOD:
                        CLIENT_FLOOD.pop(session_name, None)
                    continue
                except Exception as e:
                    user["status"] = "failed"
                    user["message_send"] = str(e)
                    failed.append(user)
                    ui_update_label(status_label, f"[-] Failed {username} by {session_name}: {e}")
                    # persist failure
                    try:
                        update_user_in_json(add_list_path, username, lambda existing: {**existing, **user})
                    except Exception:
                        pass
                    break

            await asyncio.sleep(random.uniform(4.0, 10.0))
    finally:
        # ensure this worker is removed from RUNNING_TASKS on exit
        try:
            if current_task in RUNNING_TASKS:
                RUNNING_TASKS.remove(current_task)
        except Exception:
            pass

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
        task = asyncio.create_task(add_users_worker(client_obj, users_subset, status_label, group_entity, user_list, add_list_path, session_name=session_name))
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
    try:
        safe_write_json(os.path.join(OUTPUT_DIR, f"user_added_{Path(add_list_path).stem}.json"), agg_added)
    except Exception:
        pass
    try:
        safe_write_json(os.path.join(OUTPUT_DIR, f"user_failed_{Path(add_list_path).stem}.json"), agg_failed)
    except Exception:
        pass
    try:
        safe_write_json(os.path.join(OUTPUT_DIR, f"user_pending_{Path(add_list_path).stem}.json"), agg_pending)
    except Exception:
        pass

    # final save of master add list (overwrite atomically)
    try:
        safe_write_json(add_list_path, user_list)
    except Exception:
        pass

    ui_update_label(status_label, f"Add process finished! added={len(agg_added)} failed={len(agg_failed)} pending={len(agg_pending)}")


# Send messages

async def send_messages_worker(client_obj, users, messages_list, status_label, group_link_text, all_users_ref, failed_path):
    sent, failed = [], []
    total = len(users)

    # register this worker
    current_task = asyncio.current_task()
    if current_task not in RUNNING_TASKS:
        RUNNING_TASKS.append(current_task)

    try:
        for i, user in enumerate(users, start=1):
            if STOP_EVENT.is_set():
                ui_update_label(status_label, "Stopped by user (send messages).")
                break
            uid = user.get("id")
            username = user.get("username")
            if not uid:
                ui_update_label(status_label, f"[send] skip {username} (no id)")
                failed.append(user)
                # save progress snapshot of still-failed (aggregate) atomically
                try:
                    os.makedirs(os.path.dirname(failed_path) or ".", exist_ok=True)
                    still_failed = [u for u in all_users_ref if u.get("message_send") != "sent"]
                    safe_write_json(failed_path, still_failed)
                except Exception:
                    pass
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
                for remaining in range(int(wait_time), 0, -1):
                    if STOP_EVENT.is_set():
                        ui_update_label(status_label, "Stopped by user during FloodWait (send).")
                        break
                    ui_update_label(status_label, f"[!] FloodWait while sending: waiting {remaining}s")
                    await asyncio.sleep(1)
                if STOP_EVENT.is_set():
                    break
                # retry after wait
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
            # save progress after each user (aggregate of still-failed) atomically
            try:
                still_failed = [u for u in all_users_ref if u.get("message_send") != "sent"]
                safe_write_json(failed_path, still_failed)
            except Exception:
                pass
            if STOP_EVENT.is_set():
                break
            await asyncio.sleep(random.uniform(2.0, 6.0))
    finally:
        try:
            if current_task in RUNNING_TASKS:
                RUNNING_TASKS.remove(current_task)
        except Exception:
            pass

    return {"sent": sent, "failed": failed}
# ...existing code...

# globals para stop/resume
STOP_EVENT = threading.Event()
RUNNING_TASKS = []
# novo: rastrear última operação para Resume universal
LAST_OPERATION = None        # valores: "convert", "add", "send"
LAST_OP_ARGS = {}            # dicionário com argumentos para re-agendamento

async def stop_all_operations_async():
    """
    Cancela tasks em RUNNING_TASKS, desconecta clientes e salva progresso.
    Executar via schedule_coro(stop_all_operations_async()).
    """
    # sinalizar (idem STOP_EVENT.set() chamado na UI)
    STOP_EVENT.set()

    # cancel tasks que estão rodando (workers adicionam a si mesmos em RUNNING_TASKS)
    tasks = [t for t in RUNNING_TASKS if not t.done()]
    if tasks:
        for t in tasks:
            try:
                t.cancel()
            except Exception:
                pass
        await asyncio.gather(*tasks, return_exceptions=True)

    # desconectar clientes
    for client_obj, _session in list(clients):
        try:
            await client_obj.disconnect()
        except Exception:
            pass

    # gravar estado final do ADDUSERLIST (se existir) de forma atômica
    try:
        if os.path.exists(ADDUSERLIST):
            with FILE_IO_LOCK:
                try:
                    with open(ADDUSERLIST, "r", encoding="utf-8") as fr:
                        data = json.load(fr)
                except Exception:
                    data = None
            if data is not None:
                try:
                    safe_write_json(ADDUSERLIST, data)
                except Exception:
                    pass
    except Exception:
        pass

    # limpar trackers
    RUNNING_TASKS.clear()
    return

def request_stop_from_ui(status_label=None):
    """
    Chamado pela UI: seta o STOP_EVENT e agenda a rotina que para tudo.
    """
    STOP_EVENT.set()
    if status_label:
        ui_update_label(status_label, "Stop requested...")
    try:
        schedule_coro(stop_all_operations_async())
    except Exception:
        pass

# UI wiring

def schedule_convert(status_label, input_path):
    if not clients:
        ui_update_label(status_label, "No clients available to convert (start clients first).")
        return
    # guarda operação para resume
    global LAST_OPERATION, LAST_OP_ARGS
    LAST_OPERATION = "convert"
    LAST_OP_ARGS = {"input_path": input_path, "output_path": ADDUSERLIST}
    ui_update_label(status_label, "Starting convert (building json & resolving ids)...")
    schedule_coro(convert_async(status_label, input_path=input_path, output_path=ADDUSERLIST, client_for_lookup=clients[0][0]))

def schedule_add_users(status_label):
    if not clients:
        ui_update_label(status_label, "No clients available to add users.")
        return
    global LAST_OPERATION, LAST_OP_ARGS
    LAST_OPERATION = "add"
    LAST_OP_ARGS = {"add_list_path": ADDUSERLIST, "group_link": GROUP_LINK}
    ui_update_label(status_label, "Scheduling add users...")
    schedule_coro(add_users_coordinator(status_label, add_list_path=ADDUSERLIST, group_link=GROUP_LINK))

def schedule_send_messages(status_label, messages_list, link_text):
    if not clients:
        ui_update_label(status_label, "No clients available to send messages.")
        return
    if not messages_list:
        ui_update_label(status_label, "No messages provided. Please write at least one message.")
        return
    global LAST_OPERATION, LAST_OP_ARGS
    LAST_OPERATION = "send"
    LAST_OP_ARGS = {"messages_list": messages_list, "link_text": link_text}
    ui_update_label(status_label, "Scheduling send messages...")
    schedule_coro(send_messages_coordinator(status_label, messages_list, link_text))


# Coordinator for sending messages

async def send_messages_coordinator(status_label, messages_list, link_text, add_list_path=ADDUSERLIST):
    if not clients:
        ui_update_label(status_label, "No clients available to send messages.")
        return
    if not os.path.exists(add_list_path):
        ui_update_label(status_label, f"User list not found: {add_list_path}")
        return

    with open(add_list_path, "r", encoding="utf-8") as f:
        user_list = json.load(f)

    ui_update_label(status_label, f"Starting sending messages to {len(user_list)} users using {len(clients)} accounts...")
    parts = partition_users_evenly(user_list, len(clients))
    worker_tasks = []
    failed_path = os.path.join(OUTPUT_DIR, "user_failed_send.json")
    for (client_obj, session_name), users_subset in zip(clients, parts):
        if not users_subset:
            continue
        task = asyncio.create_task(send_messages_worker(client_obj, users_subset, messages_list, status_label, link_text, user_list, failed_path))
        worker_tasks.append(task)

    results = await asyncio.gather(*worker_tasks, return_exceptions=True)
    agg_sent, agg_failed = [], []
    for r in results:
        if isinstance(r, Exception):
            ui_update_label(status_label, f"Worker error: {r}")
            continue
        agg_sent.extend(r.get("sent", []))
        agg_failed.extend(r.get("failed", []))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    try:
        safe_write_json(os.path.join(OUTPUT_DIR, "user_sent.json"), agg_sent)
    except Exception:
        pass
    try:
        safe_write_json(os.path.join(OUTPUT_DIR, "user_failed_send.json"), agg_failed)
    except Exception:
        pass

    ui_update_label(status_label, f"Send process finished! sent={len(agg_sent)} failed={len(agg_failed)}")

def find_input_txt_blocks(input_dir="process/input", base_name="userBase"):
    """
    Return ordered list of .txt block paths:
    userBase.txt, userBase1.txt, userBase2.txt, ...
    """
    p = Path(input_dir)
    if not p.exists():
        return []
    items = []
    for f in p.iterdir():
        if not f.is_file() or not f.name.endswith(".txt"):
            continue
        name = f.stem  # without .txt
        if name == base_name:
            items.append((0, str(f)))
            continue
        if name.startswith(base_name):
            suffix = name[len(base_name):]
            m = re.match(r"^(\d+)$", suffix)
            if m:
                items.append((int(m.group(1)), str(f)))
    items.sort(key=lambda x: x[0])
    return [fp for _i, fp in items]

def find_input_json_blocks(input_dir="process/input", base_name="userBase"):
    """
    Return ordered list of .json block paths in input dir:
    userBase.json, userBase1.json, ...
    """
    p = Path(input_dir)
    if not p.exists():
        return []
    items = []
    for f in p.iterdir():
        if not f.is_file() or not f.name.endswith(".json"):
            continue
        name = f.stem
        if name == base_name:
            items.append((0, str(f)))
            continue
        if name.startswith(base_name):
            suffix = name[len(base_name):]
            m = re.match(r"^(\d+)$", suffix)
            if m:
                items.append((int(m.group(1)), str(f)))
    items.sort(key=lambda x: x[0])
    return [fp for _i, fp in items]

async def convert_block_file(status_label, input_txt_path, output_json_path, client_for_lookup):
    """
    Convert a single .txt block into a JSON (saved inside process/input).
    Resolve IDs one-by-one and persist per-user.
    After conversion, move .txt to process/processed.
    """
    ui_update_label(status_label, f"Converting {os.path.basename(input_txt_path)} -> {os.path.basename(output_json_path)}")
    os.makedirs(os.path.dirname(output_json_path) or ".", exist_ok=True)

    # build initial list from txt while respecting existing json content
    disk_map = {}
    if os.path.exists(output_json_path):
        try:
            with open(output_json_path, "r", encoding="utf-8") as fr:
                existing = json.load(fr)
                if isinstance(existing, list):
                    disk_map = {u.get("username"): u for u in existing if u.get("username")}
        except Exception:
            disk_map = {}

    user_list = []
    seen = set()
    with open(input_txt_path, "r", encoding="utf-8") as fr:
        for line in fr:
            username = line.strip()
            if not username or username in seen:
                continue
            seen.add(username)
            # skip if disk already has pending/failed per your rule (avoid re-resolve)
            if username in disk_map and disk_map[username].get("status") in ("pending", "failed"):
                continue
            if username in disk_map and disk_map[username].get("id"):
                entry = disk_map[username]
                entry.setdefault("status", "pending")
                entry.setdefault("message_send", entry.get("message_send", ""))
                user_list.append(entry)
            else:
                user_list.append({"username": username, "id": None, "status": "pending", "message_send": ""})

    # resolve ids (one-by-one) and persist per-user into output_json_path
    await fetch_user_ids_async(client_for_lookup, status_label, user_list, output_path=output_json_path)

    # ensure merged final JSON in input folder
    try:
        disk_entries = []
        if os.path.exists(output_json_path):
            with open(output_json_path, "r", encoding="utf-8") as fr:
                disk_entries = json.load(fr) or []
        disk_map2 = {u.get("username"): u for u in disk_entries if u.get("username")}
        for u in user_list:
            if u.get("username"):
                disk_map2[u["username"]] = u
        merged = list(disk_map2.values())
        safe_write_json(output_json_path, merged)
    except Exception:
        pass

    # move processed txt to processed folder
    try:
        processed_dir = os.path.join("process", "processed")
        os.makedirs(processed_dir, exist_ok=True)
        shutil.move(input_txt_path, os.path.join(processed_dir, os.path.basename(input_txt_path)))
        ui_update_label(status_label, f"Moved {os.path.basename(input_txt_path)} -> process/processed")
    except Exception:
        pass

async def convert_all_blocks(status_label, input_dir="process/input", base_name="userBase"):
    """
    Find all input txt blocks and convert each to a JSON placed next to the txt
    (process/input/userBaseN.json). Uses first available client for lookups.
    """
    txt_blocks = find_input_txt_blocks(input_dir=input_dir, base_name=base_name)
    if not txt_blocks:
        ui_update_label(status_label, "No input txt blocks found.")
        return
    if not clients:
        ui_update_label(status_label, "No clients available to resolve IDs (start clients first).")
        return
    client_for_lookup = clients[0][0]
    for txt_path in txt_blocks:
        stem = Path(txt_path).stem  # e.g., userBase or userBase1
        json_name = f"{stem}.json"
        json_path = os.path.join(input_dir, json_name)
        await convert_block_file(status_label, txt_path, json_path, client_for_lookup)
    ui_update_label(status_label, "All txt blocks converted to JSON in process/input.")

async def add_all_blocks_coordinator(status_label, input_dir="process/input", output_dir=OUTPUT_DIR, group_link=GROUP_LINK):
    """
    Process all userBase*.json found in process/input sequentially (one file at a time).
    After each JSON is fully added, move it to process/output to mark processed.
    """
    if not clients:
        ui_update_label(status_label, "No clients available to add users.")
        return
    json_blocks = find_input_json_blocks(input_dir=input_dir, base_name="userBase")
    if not json_blocks:
        ui_update_label(status_label, "No JSON blocks found in process/input.")
        return

    for json_path in json_blocks:
        ui_update_label(status_label, f"Adding users from {os.path.basename(json_path)} ...")
        # reuse existing coordinator (it partitions users among clients)
        await add_users_coordinator(status_label, add_list_path=json_path, group_link=group_link)
        # after processing, move JSON to output (archive processed)
        try:
            os.makedirs(output_dir, exist_ok=True)
            dest = os.path.join(output_dir, os.path.basename(json_path))
            shutil.move(json_path, dest)
            ui_update_label(status_label, f"Moved {os.path.basename(json_path)} -> {output_dir}")
        except Exception as e:
            ui_update_label(status_label, f"Could not move {json_path} to output: {e}")

    ui_update_label(status_label, "All JSON blocks processed and moved to output.")

def build_ui():
    root = Tk()
    root.title("Telegram Multi-Account Adder")
    root.geometry("1000x720")
    notebook = ttk.Notebook(root)
    notebook.pack(expand=True, fill="both", padx=8, pady=8)

    # --- Tab 1: Files / Actions ---
    frame_files = Frame(notebook)
    notebook.add(frame_files, text="Files / Actions")

    lbl_input_dir = Label(frame_files, text="Input dir: process/input (expects userBase*.txt/json)")
    lbl_input_dir.pack(anchor="w", padx=8, pady=(8,0))

    lbl_status_files = Label(frame_files, text="Ready", font=("Arial", 12))
    lbl_status_files.pack(pady=6)
    log_files = scrolledtext.ScrolledText(frame_files, height=14)
    log_files.pack(fill="both", padx=8, pady=6, expand=True)

    def on_convert_blocks_clicked():
        ui_update_label(lbl_status_files, "Scheduling convert of all txt blocks...")
        ui_append_text(log_files, "[+] Scheduled txt -> json (blocks)")
        schedule_coro(convert_all_blocks(lbl_status_files))

    def on_add_blocks_clicked():
        ui_update_label(lbl_status_files, "Scheduling add of all json blocks (process/input)...")
        ui_append_text(log_files, "[+] Scheduled Add All Blocks")
        schedule_coro(add_all_blocks_coordinator(lbl_status_files))

    btn_convert_blocks = Button(frame_files, text="Txt -> JSON Blocks", command=on_convert_blocks_clicked, fg="blue")
    btn_convert_blocks.pack(pady=6, padx=8, anchor="w")
    btn_add_blocks = Button(frame_files, text="Add All Blocks", command=on_add_blocks_clicked, fg="blue")
    btn_add_blocks.pack(pady=6, padx=8, anchor="w")

    # --- Tab 2: Send Messages (kept similar) ---
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

    # --- Tab 3: Clients status ---
    frame_clients = Frame(notebook)
    notebook.add(frame_clients, text="Clients")
    lbl_clients = Label(frame_clients, text="Clients / Flood status:")
    lbl_clients.pack(anchor="w", padx=8, pady=(8,0))
    txt_clients = scrolledtext.ScrolledText(frame_clients, height=18)
    txt_clients.pack(fill="both", padx=8, pady=6, expand=True)

    async def clients_status_loop():
        while True:
            lines = []
            if not clients:
                lines.append("No clients started.")
            else:
                for client_obj, session_name in clients:
                    flood_until = CLIENT_FLOOD.get(session_name)
                    status = "OK"
                    if flood_until:
                        rem = max(0, int(flood_until - time.time()))
                        status = f"FLOODED ({rem}s)"
                    # try to show username/id if available
                    try:
                        who = None
                        # avoid blocking: use cached session name only
                        lines.append(f"{session_name}: {status}")
                    except Exception:
                        lines.append(f"{session_name}: {status}")
            def _set():
                txt_clients.delete("1.0", END)
                for L in lines:
                    txt_clients.insert(END, L + "\n")
                txt_clients.see(END)
            txt_clients.after(0, _set)
            await asyncio.sleep(2)

    try:
        schedule_coro(clients_status_loop())
    except Exception:
        pass

    return root


# Main

if __name__ == "__main__":
    # Construir UI e proteger com try/except para capturar erros que impedem a abertura da janela
    try:
        print("[INFO] Building UI...")
        ui_root = build_ui()

        # schedule start_all_clients_safe AFTER UI is construída (evita bloqueios/contenda no startup)
        try:
            schedule_coro(start_all_clients())
        except Exception as e:
            print("[WARN] scheduling start_all_clients_safe failed:", e)

        print("[INFO] Starting Tk mainloop...")

        # handler de fechamento gracioso
        def on_close():
            try:
                request_stop_from_ui()
            except Exception:
                pass

            def try_shutdown():
                # se ainda há workers, aguardar um pouco mais
                if RUNNING_TASKS:
                    ui_root.after(500, try_shutdown)
                    return
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except Exception:
                    pass
                ui_root.destroy()

            ui_root.after(500, try_shutdown)

        ui_root.protocol("WM_DELETE_WINDOW", on_close)
        ui_root.mainloop()
    except Exception as e:
        import traceback
        print("[ERROR] UI failed to start:", e)
        traceback.print_exc()
        # Mantém o processo vivo para inspecionar logs
        while True:
            time.sleep(60)
