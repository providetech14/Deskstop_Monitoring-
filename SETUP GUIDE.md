# WorkLog: Attendance & Work Monitoring

WorkLog replaces the paper log book. Employees record **Time In, Start Break, End Break and Time Out** themselves, on their work PC or phone. HR sees everything live, with activity levels and screenshots from the work PCs.

## Current status of this laptop (already set up)

| Item | Status |
|---|---|
| WorkLog server | Installed in **`C:\WorkLog`**. Runs hidden in the background, starts automatically when you log in to Windows, and Windows Task Scheduler checks every 5 minutes and restarts it if it stopped |
| Your address for employees | Depends on the Wi-Fi network. Always shown, with a QR code, in **WorkLog → Settings** (currently http://192.168.1.31:5000) |
| Firewall | Allows PCs and phones on your **local network only** (TCP 5000, UDP 5001) |
| Sleep | Laptop **never sleeps while plugged in** (on battery it still sleeps after 10 min) |
| Backups | Automatic every day, last 30 kept, in `C:\WorkLog\server\data\backups` |
| Desktop icon | **WorkLog (HR)** opens the system |

## Step 1: Create your HR Admin account (once)

Double-click **WorkLog (HR)** on the desktop (or open http://localhost:5000). The first time, it asks for the company name and **your** username and password. This becomes the HR Admin account.

## Step 2: Add your employees

**Employees** page: add each person's full name, position, username and a temporary password. Choose role **HR Admin** for anyone else who should have HR access.

## Step 3: Install the agent on each work PC

1. Copy the whole **`Employee Installer`** folder to the work PC (USB drive or shared folder).
2. Double-click **`Install WorkLog Agent.bat`**.
   - If Windows says *"Windows protected your PC"*, click **More info**, then **Run anyway**. This appears because the app is new, not because something is wrong.
3. WorkLog opens with your address already filled in. The employee logs in with their username and password.

The agent then starts with Windows, puts a **WorkLog** icon on the desktop, and **finds your laptop automatically even if its address changes**. No admin rights or Python needed on work PCs. To remove it, run `Uninstall WorkLog Agent.bat`.

## Step 4: Phones

On the office Wi-Fi, employees scan the **QR code** in WorkLog → Settings (or type the address) and log in. Tip: "Add to Home screen" makes it work like an app.

---

## Daily use

**Employees:** Time In, then Start Break / End Break, then Time Out (agent window, website or phone). Closing the agent window only minimizes it.

**The agent reminds them automatically:**
- after work start time if they haven't timed in (every 15 min, work days only)
- after work end time if they're still timed in
- when a break runs longer than the break allowance
- **🔊 Voice reminder:** if they're timed in but haven't used the keyboard or mouse for **1 minute**, the work PC **says out loud** *"Hello {name}. You have not used your computer for 1 minute. Please get back to work, or click Start Break if you are on a break."* An "Are you still there?" window also appears, with **I'm back** and **Start Break** buttons. It repeats every minute until they're back and closes by itself when they use the PC again. No reminders during breaks or after Time Out. HR can change the minutes (0 = off) and the words in **Settings**. The Live Board shows how many reminders each person got today.

**Screenshots** are taken **every 5 minutes** while timed in (not on break). You can change this in Settings.

**HR pages:**
- **Live Board**: who is Working, On Break, Timed Out or Not yet timed in; late minutes; hours worked; break time (flags over-break); **Activity %** and **Idle** warnings; whether the agent is online; latest screenshot. Refreshes every minute.
- **Attendance**: any date range; days present, **absent days**, late days, missing time-outs, worked/break totals, activity. **Download Excel (CSV)** for payroll. **Add a correction** when someone forgot to punch (labelled with your name).
- **Screenshots**: by date and employee; click to enlarge.
- **Employees**: add, edit, reset password, deactivate (history is kept).
- **Settings**: company name, work hours, **work days**, late grace period, break allowance, screenshot interval and retention, phone QR code, backups.

**What Activity % means:** the share of working minutes in which the keyboard or mouse was used on the work PC. **What was typed is never recorded.** Low activity is a prompt to check, not proof: phone calls, meetings and reading also count as idle.

**Forgot to time out?** The employee can still time in the next day. The missed day shows **"No time out"** and only counts hours up to the official end of work. Add a correction if needed.

## Keep in mind

- **The laptop is the server.** It must be **on, logged in, plugged in and connected to the office Wi-Fi** during work hours. Don't close the lid, because that puts it to sleep. If it's off, nobody can clock in. (Agents keep retrying and reconnect by themselves.)
- **Phones need the current address.** If the router gives the laptop a new address, the agents adjust automatically but phones need the new address (see Settings). Ask whoever manages the router to **reserve a fixed IP** for this laptop.
- **Privacy (Data Privacy Act, RA 10173):** inform employees in writing that screenshots and active/idle time are recorded while timed in (never during breaks or after Time Out), who can see them, and how long they're kept. Have them sign the policy. The agent shows this notice on screen.
- **Backups:** daily backups are automatic, but they're on the same laptop. Once a week, copy `C:\WorkLog\server\data` to a USB drive or cloud storage.

## Controls (Start menu → WorkLog)

| Shortcut | Use |
|---|---|
| WorkLog (HR) | Open the system |
| Restart WorkLog Server | If something seems stuck |
| Stop / Start WorkLog Server | Turn it off/on manually |
| WorkLog folder | Opens `C:\WorkLog` (data, logs, backups) |

## Troubleshooting

| Problem | Fix |
|---|---|
| Employee PC/phone can't connect | Laptop on and on the same Wi-Fi? Try **Restart WorkLog Server**. Check the address in Settings. |
| Employee forgot password | Employees → Edit → Reset password |
| Someone left the company | Employees → Edit → Deactivate |
| Agent shows "already running" | It's minimized. Click the WorkLog icon in the taskbar. |
| Need to restore data | Stop the server, copy a file from `data\backups` over `data\worklog.db`, then start the server |
| Logs for IT | `C:\WorkLog\server\data\logs\` |

## For IT: project folder layout

| Folder / file | Purpose |
|---|---|
| `Employee Installer\` | Ready-to-copy package for work PCs |
| `Install or Update WorkLog on this laptop.bat` | Re-installs or updates the server in `C:\WorkLog` (keeps data), the firewall rules and the Employee Installer |
| `server\` | Server source (Python, Flask + Waitress, SQLite) |
| `agent\` | Agent source (`worklog_agent.py`, Python standard library only) |
| `installer\` | Install/uninstall scripts copied into the Employee Installer |
| `tools\` | Build and install scripts |

If the laptop's address changes permanently, run `tools\build_employee_installer.ps1` (or the "Install or Update" file) to refresh `server.txt` before installing on new PCs.
