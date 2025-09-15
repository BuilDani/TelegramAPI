# TODO: Modify main.py for User Addition from userBase.json

## Steps:
1. Add client selection dialog at startup using Tkinter to choose one client.
2. Modify start_all_clients to return the clients list.
3. Update add_users_coordinator to:
   - Read users from userBase.json, extracting only 'username' and 'id'.
   - Process each user with the selected client.
   - On success, remove from userBase.json and append to user_added.json.
   - On failure, remove from userBase.json and append to user_failed.json.
   - Use shorter delays (1-3 seconds) but respect floodwait.
4. Simplify UI: Remove the "Convert TXT -> JSON" tab, update "Add Users" tab to use selected client and group link.
5. Test the changes for proper functionality.

## Status:
- [ ] Step 1: Add client selection dialog
- [ ] Step 2: Modify start_all_clients
- [ ] Step 3: Update add_users_coordinator
- [ ] Step 4: Simplify UI
- [ ] Step 5: Test
