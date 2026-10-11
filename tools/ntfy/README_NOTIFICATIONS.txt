=====================================================================
  Aether BBS — Push Notifications via ntfy
=====================================================================

OVERVIEW
--------
ntfy (pronounced "notify") is a free, open-source push notification
server. It runs on your PC alongside the BBS and sends alerts to
your phone over your local WiFi network. No cloud, no accounts,
no internet required.

When a priority message arrives at the BBS (addressed to one of
your tactical callsigns), your phone will buzz with a notification
showing who sent the message and what it's about.


WHAT YOU NEED
-------------
- This PC (where Aether BBS runs) — acts as the ntfy server
- A phone on the same WiFi network — receives notifications
- The free "ntfy" app installed on your phone


SETUP (5 MINUTES)
------------------

  Step 1: Install the ntfy server
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  Double-click:  tools\ntfy\setup_ntfy.bat

  This downloads the ntfy server (~10 MB), configures it for
  local-network use, and sets it to start automatically when
  you log in to Windows. It will display your PC's LAN IP
  address when finished — write this down.


  Step 2: Install the phone app
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  - Android: Search "ntfy" in Google Play Store
  - iPhone:  Search "ntfy" in Apple App Store

  Install the free app by Philipp C. Heckel.


  Step 3: Subscribe to alerts
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  Open the ntfy app on your phone and:

  1. Tap the "+" button
  2. Tap "Use another server" (important!)
  3. Enter your PC's address:  http://<YOUR-PC-IP>:8080
  4. For the topic, enter:     aether-bbs-alerts
  5. Tap Subscribe

  Replace <YOUR-PC-IP> with the IP shown by setup_ntfy.bat
  (typically something like 192.168.1.100).


  Step 4: Enable in Aether BBS
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  In Aether BBS:

  1. Go to Settings > Notifications
  2. Check "Enable push notifications via ntfy"
  3. Server URL should be: http://127.0.0.1:8080
  4. Topic should be: aether-bbs-alerts
  5. Click Save


  Step 5: Test it
  ~~~~~~~~~~~~~~~~
  In Settings > Notifications, click "Send Test Notification".
  Your phone should buzz within a few seconds.


MULTIPLE PHONES
---------------
Multiple people can subscribe to the same topic. Just repeat
Steps 2-3 on each phone. Everyone on the team gets alerted.


TROUBLESHOOTING
---------------
  Phone doesn't receive notifications?

  1. Make sure your phone is on the same WiFi network as the PC.
  2. Verify ntfy is running: open a browser on your phone and
     go to http://<YOUR-PC-IP>:8080 — you should see the ntfy
     web interface.
  3. Check Windows Firewall: ntfy needs port 8080 open for
     inbound connections. The setup script does not modify
     firewall rules. You may need to add an exception:
       Windows Security > Firewall > Advanced Settings >
       Inbound Rules > New Rule > Port > TCP 8080 > Allow
  4. On iPhone, ntfy polls for messages by default (up to 15
     minutes delay due to iOS restrictions). For faster delivery,
     keep the ntfy app open or use the ntfy.sh relay (requires
     internet — see ntfy docs for "iOS instant notifications").
  5. On Android, make sure the ntfy app is excluded from battery
     optimization so it can receive messages in the background.
  6. Check Aether BBS logs for "ntfy delivery failed" messages.


MANUAL START / STOP
-------------------
If the auto-start task doesn't work, you can:

  Start:  Double-click tools\ntfy\start_ntfy.bat
  Stop:   Double-click tools\ntfy\stop_ntfy.bat

To remove the auto-start task:
  Open Task Scheduler and delete "Aether-BBS-ntfy" (or "VARA-BBS-ntfy"
  if it was set up before the rename to Aether BBS)


ADVANCED: CHANGING THE PORT
----------------------------
Edit tools\ntfy\server.yml and change the listen-http line:
  listen-http: ":9090"

Then update the URL in both the Aether BBS settings and your
phone's subscription.


MORE INFORMATION
----------------
ntfy documentation: https://docs.ntfy.sh
ntfy source code:   https://github.com/binwiederhier/ntfy

=====================================================================
  73 de KK4ODA
=====================================================================
