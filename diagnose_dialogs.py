"""Counts every dialog on the account, by type and folder. Read-only.

Nothing is downloaded and nothing is written except Telethon's own session
bookkeeping. Use it to confirm that the number of groups the app lists matches
what your account actually has.

    .venv/bin/python diagnose_dialogs.py

Close the GUI app first. Both share one SQLite session file, and two clients
writing it at once can corrupt it.
"""
import asyncio
import sys

from telethon import TelegramClient, errors

from interactive_downloader import ChatEntry, load_account_store


async def walk(client, archived, into, label):
    """Pages one folder into `into`, surviving rate limits."""
    attempts = 0
    while True:
        try:
            async for d in client.iter_dialogs(limit=None, archived=archived):
                if d.id not in into:
                    into[d.id] = ChatEntry.from_dialog(d)
                    if len(into) % 50 == 0:
                        print(f"  ... {len(into)} so far", flush=True)
            return
        except errors.FloodError as e:
            wait = int(getattr(e, "seconds", 0) or 0)
            attempts += 1
            if attempts > 3:
                print(f"  ! gave up on the {label} folder after {attempts} rate limits")
                return
            print(f"  ! rate limited, waiting {wait}s (attempt {attempts})", flush=True)
            await asyncio.sleep(wait + 1)


async def main():
    accounts = load_account_store()
    profile = accounts.data["profiles"].get(accounts.data["active"])
    if not profile:
        print("No active account. Sign in from the app's Accounts page first.")
        return 1
    client = TelegramClient(profile["session"], profile["api_id"], profile["api_hash"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("This session is not signed in. See the README.")
            return 1

        found: dict = {}
        for label, archived in (("main", False), ("archive", True)):
            print(f"Walking the {label} folder...", flush=True)
            before = len(found)
            await walk(client, archived, found, label)
            print(f"  {len(found) - before} new ({len(found)} total)")

        entries = list(found.values())
        print(f"\n{'=' * 52}\n{len(entries)} dialogs total\n")
        for kind in ("Group", "Channel", "DM", "Bot"):
            same = [e for e in entries if e.kind == kind]
            if same:
                arch = sum(1 for e in same if e.archived)
                print(f"  {kind:<8} {len(same):>5}" + (f"   ({arch} archived)" if arch else ""))

        groups = [e for e in entries if e.kind in ("Group", "Channel")]
        print(f"\nThe app's Step 1 list shows {len(groups)} groups/channels by default "
              f"({len(entries)} with DMs and bots enabled).")
        print("\nGroups and channels:")
        for e in sorted(groups, key=lambda x: x.name.lower()):
            print(f"  {'[archived] ' if e.archived else ''}{e.name}")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
