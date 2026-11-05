"""The service desk knowledge base: thirty MSP runbook articles and an idempotent seeder.

Articles are written for both audiences that read them. Engineers follow the
steps, and the triage model quotes safe customer steps in its draft replies.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import rag, store
from .db import Connection

logger = logging.getLogger(__name__)

# Any constant works. It only has to be the same for every instance.
KB_SEED_LOCK_KEY = 7_401_000_012
SEED_EMBED_TIMEOUT_SECONDS = 15.0
SEED_BATCH_SIZE = 2
# Stay well inside the 60 second function limit when seeding runs from /api/health.
SEED_TIME_BUDGET_SECONDS = 25.0
# After a failed run, the health check waits this long before seeding again.
FAILURE_COOLDOWN_SECONDS = 600.0

_cooldown_until = 0.0


@dataclass(frozen=True)
class Article:
    slug: str
    title: str
    category: str
    content: str

    def embedding_text(self) -> str:
        return f"{self.title}\n\n{self.content}"


KB_ARTICLES: tuple[Article, ...] = (
    Article(
        slug="vpn-error-809",
        title="VPN error 809 or L2TP connection fails",
        category="Network",
        content="""Error 809 means the VPN client could not reach the VPN server. It is usually caused by a home router or public network blocking the ports that L2TP over IPsec needs (UDP 500 and 4500), or by the server sitting behind NAT. Most cases are fixed on the user side.

1. Confirm the user has a working internet connection by opening a public website.
2. Ask what changed recently, such as a new home router, a hotel or guest network, or a mobile hotspot.
3. Restart the home router and try again, using a wired connection if possible.
4. On the router, turn on IPsec passthrough or VPN passthrough. It is usually under the security or firewall settings.
5. Try a mobile hotspot. If the VPN connects there, the home router is blocking it.
6. If the server is behind NAT, an engineer can set the AssumeUDPEncapsulationContextOnSendRule registry value to 2 under HKLM\\SYSTEM\\CurrentControlSet\\Services\\PolicyAgent and restart the laptop.

Escalate to Infrastructure L2 if the VPN fails on every network, or if several users report error 809 at the same time, as this points to the VPN server or the firewall.""",
    ),
    Article(
        slug="m365-mfa-reset",
        title="Reset Microsoft 365 MFA for a locked out user",
        category="Access & Identity",
        content="""Users lose their MFA method when they change phone, reinstall Microsoft Authenticator or lose a device. Never reset MFA on the strength of an email alone, because attackers use this request to take over accounts.

1. Verify the user by calling back the phone number held in the client record, not a number given in the ticket.
2. For directors and finance staff, also confirm the request with a second named contact at the client.
3. In the Microsoft Entra admin centre, open Users, select the user and choose Authentication methods.
4. Select Require re-register multifactor authentication, or delete the lost phone or Authenticator entry.
5. Revoke existing sessions so that any stolen session token stops working.
6. Ask the user to sign in at aka.ms/mfasetup and register Microsoft Authenticator on the new phone.
7. Confirm they can sign in to Outlook and Teams, then record how their identity was verified in the ticket.

If the user also reports sign-ins they do not recognise, treat the ticket as a security incident and pass it to Security Ops.""",
    ),
    Article(
        slug="outlook-profile-rebuild",
        title="Rebuild a corrupt Outlook profile",
        category="Email & M365",
        content="""A damaged Outlook profile causes repeated password prompts, a stuck Trying to connect status, missing folders or Outlook closing as it starts. Rebuilding the profile is safe for Microsoft 365 mailboxes because the mail is stored in Exchange Online.

1. Check Microsoft 365 service health first. If Exchange Online has an incident, wait rather than rebuild.
2. Confirm the user can open their mailbox in Outlook on the web. If that also fails, the problem is the account and not the profile.
3. Close Outlook. Open Control Panel, choose Mail (Microsoft Outlook) and select Show Profiles.
4. Select Add, name the new profile and sign in with the user's Microsoft 365 address.
5. Set Always use this profile to the new profile and select OK.
6. Open Outlook and let the mailbox download. A large mailbox can take an hour on a slow connection.
7. Add back any shared mailboxes or calendars that were added by hand, then remove the old profile.

Local PST files are not part of the profile. Reattach them in Outlook through File, then Open and Export.""",
    ),
    Article(
        slug="shared-mailbox-access",
        title="Grant access to a shared mailbox",
        category="Email & M365",
        content="""Shared mailboxes such as accounts@ or info@ need no licence and are controlled by permissions. Only grant access when the request comes from the mailbox owner or a manager named in the client record.

1. Confirm who approved the request and record their name in the ticket.
2. In the Exchange admin centre, open Recipients, then Mailboxes, and select the shared mailbox.
3. Under Delegation, add the user to Read and manage (Full Access) so they can open the mailbox.
4. Add Send as if they need to send from the shared address, or Send on behalf if replies should show their own name.
5. Allow up to an hour for the change to apply. The mailbox then appears in Outlook automatically.
6. If it does not appear, ask the user to restart Outlook, or add the mailbox by hand in Account settings.
7. If sent items should stay in the shared mailbox rather than the user's own, turn on the setting that saves sent items to the shared mailbox.

Remove access promptly when a user leaves or changes role, and note the change in the ticket.""",
    ),
    Article(
        slug="printer-offline-network",
        title="Network printer shows as offline",
        category="Hardware",
        content="""When a network printer shows as offline on every laptop, the printer has usually changed IP address, dropped off the network or gone into deep sleep. When it is offline on one laptop only, the problem is on that laptop.

1. Check the printer display for errors such as a paper jam, empty toner or an open tray.
2. Print a configuration page from the printer menu and note its IP address.
3. On a laptop, open Printers and scanners, then Printer properties, then Ports, and compare the address. A different address means the printer took a new one from DHCP.
4. Update the port to the new address. For a lasting fix, ask Infrastructure L2 to reserve a fixed address for the printer.
5. If the printer has no IP address, check its network cable and the switch port lights, then restart the printer.
6. If only one laptop is affected, restart the Print Spooler service, then remove and add the printer again.

Escalate to Infrastructure L2 if several printers drop off at once, as that suggests a network fault rather than a printer fault.""",
    ),
    Article(
        slug="office-wifi-drops",
        title="Wi-Fi keeps dropping in the office",
        category="Network",
        content="""Intermittent Wi-Fi is usually caused by weak signal, interference from neighbouring networks, too many devices on one access point or an out of date wireless driver. First find out whether the problem affects one device, one area or everyone.

1. Ask which devices drop, where in the building and at what times of day.
2. If one laptop drops, update its wireless driver from the manufacturer's support site and turn off power saving for the wireless adapter.
3. On that device, forget the network and connect again.
4. If one area drops, check the signal there. Fewer than two bars usually means an access point is needed nearer that area.
5. If everyone drops, check the access point management portal for reboots, firmware alerts or crowded channels.
6. Move access points to quieter channels and prefer 5 GHz for laptops.
7. Check whether the drops match the times that the broadband line or the firewall restarts.

Escalate to Infrastructure L2 with the device names, locations and times collected, so they can review the wireless controller logs.""",
    ),
    Article(
        slug="ransomware-first-response",
        title="Ransomware first response",
        category="Security",
        content="""Treat a ransom note, files with a strange new extension or files that suddenly cannot be opened as a live ransomware incident. Speed matters more than diagnosis, and this is always a P1 for Security Ops.

1. Tell the user to disconnect the affected device from the network straight away by unplugging the cable and turning off Wi-Fi.
2. Do not switch the device off. Its memory can hold evidence and sometimes the encryption keys.
3. Do not pay the ransom and do not contact the attackers.
4. Ask the client to stop using shared drives until Security Ops confirm the scope.
5. Escalate to Security Ops by phone as well as in the ticket, and start an incident timeline.
6. Disable the affected user accounts and any accounts used on the device.
7. Confirm that backups are offline or immutable, and protect them from the network.
8. Record the ransom note text, the new file extension and the time the problem was first seen.

If personal data may be involved, the client may need to report the incident to the ICO within 72 hours. The account manager will advise them on this.""",
    ),
    Article(
        slug="phishing-link-clicked",
        title="User clicked a phishing link or entered credentials",
        category="Security",
        content="""A clicked phishing link is a security incident even when nothing seems to have happened. If the user typed a password into the page, assume the account is compromised and act within minutes.

1. Ask the user not to delete the email and to forward it to the service desk as an attachment.
2. If credentials were entered, reset the password now and revoke all sessions in the Microsoft Entra admin centre.
3. Check the user's MFA methods and remove any method the user did not add.
4. Review the sign-in logs for unfamiliar locations or devices since the email arrived.
5. Check the mailbox for new inbox rules or forwarding addresses, which attackers use to hide replies.
6. If a file was downloaded or opened, isolate the device and run a full antivirus scan.
7. Search other mailboxes for the same email and remove every copy.
8. For finance staff, warn the client to confirm any change of bank details or payment request by phone.

Pass the ticket to Security Ops if there are suspicious sign-ins, new inbox rules or any sign of malware.""",
    ),
    Article(
        slug="backup-restore-request",
        title="Restore a file or folder from backup",
        category="Backup & Recovery",
        content="""Most deleted or overwritten files can be recovered. Check the quick options first, then restore from backup.

1. Confirm the exact file or folder name, where it was stored and the date and time it was last correct.
2. For OneDrive or SharePoint files, check the recycle bin first. Deleted items stay there for 93 days.
3. For an overwritten OneDrive or SharePoint file, use Version history to restore an earlier version.
4. For files on a server or NAS, try Previous Versions in File Explorer if shadow copies are turned on.
5. Otherwise open the backup console, choose the restore point just before the date given and restore to a separate location.
6. Ask the user to check the restored copy before it replaces anything.
7. Record the restore point used and the time taken in the ticket.

If the restore is needed because of ransomware or because a large number of files vanished at once, stop and escalate to Security Ops and Infrastructure L2. The cause must be contained before any data is restored, or the restored files may be lost again.""",
    ),
    Article(
        slug="new-starter-onboarding",
        title="New starter onboarding checklist",
        category="Access & Identity",
        content="""A complete onboarding request needs at least five working days of notice. Ask the client for any missing details before starting work.

1. Collect the full name, job title, start date, manager, department and any shared mailboxes or Teams the starter needs.
2. Create the user in Microsoft 365, assign the correct licence and add them to the right security and distribution groups.
3. Set a temporary password and require a password change and MFA registration at first sign-in. Give the password to the manager by phone or text message, never in the same email as the username.
4. Prepare the laptop with the standard build, enrol it in Intune and install the line of business applications.
5. Grant access to shared drives, printers and finance or CRM systems as approved by the manager.
6. Book a short call on the first morning to help the starter sign in and set up MFA.
7. Close the ticket only when the starter confirms they can work.

For a leaver, follow the same list in reverse on the agreed last day, and convert their mailbox to a shared mailbox if the manager needs it.""",
    ),
    Article(
        slug="laptop-slow-disk-full",
        title="Laptop running slowly or disk full",
        category="Hardware",
        content="""A slow laptop is usually caused by a nearly full disk, too many startup apps, pending updates or failing hardware. Windows slows down badly when less than 10 per cent of the disk is free.

1. Restart the laptop. Many slowness reports clear after a restart that finishes pending updates.
2. Check the free space in File Explorer under This PC.
3. Run Disk Cleanup or Storage Sense to remove temporary files and old update files.
4. Ask the user to move large personal files into OneDrive and set them to online only.
5. Open Task Manager, look for processes using a lot of CPU, memory or disk, and turn off startup apps that are not needed.
6. Make sure Windows Update and the antivirus have finished their scans.
7. If the laptop is still slow, check the disk health with the manufacturer's diagnostics.

If the laptop is more than four years old or has less than 8 GB of memory, recommend a replacement to the account manager. If unknown processes are using a lot of CPU, raise a security ticket for Security Ops.""",
    ),
    Article(
        slug="password-reset-self-service",
        title="Self-service password reset",
        category="Access & Identity",
        content="""Users who are registered for self-service password reset can reset a forgotten or expired Microsoft 365 password without waiting for the service desk.

1. Ask the user to go to aka.ms/sspr on any device, or to select Forgot my password on the sign-in page.
2. They enter their work email address and complete the image check.
3. They verify with two of their registered methods, for example the Authenticator app and a text message.
4. They choose a new password of at least 14 characters. A phrase of three unrelated words works well.
5. After the reset, they should sign in again on their phone and laptop so that mail and Teams reconnect.
6. If the account is locked rather than the password forgotten, they can wait 15 minutes or ask the service desk to unlock it.

If the user is not registered, the service desk must verify their identity by calling back a known number before setting a temporary password. Remind users to register their methods at aka.ms/mfasetup, and never ask anyone to send a password by email or chat.""",
    ),    Article(
        slug="intune-enrolment-fails",
        title="Intune enrolment fails or device shows as not compliant",
        category="Software",
        content="""A laptop that will not enrol in Microsoft Intune, or that enrols but shows as not compliant, is blocked from email and Teams by conditional access. Most failures come from licensing, enrolment restrictions or a device that is already registered elsewhere.

1. Confirm the user has an Intune licence in the Microsoft 365 admin centre.
2. Note the error code shown in Settings, Accounts, Access work or school, or in the Company Portal app.
3. In the Intune admin centre, check the enrolment restrictions allow Windows devices and that the user has not reached the device limit.
4. Look for a stale record of the same device in Intune and in Microsoft Entra ID, delete it and enrol again.
5. For Autopilot devices, check the hardware hash is imported and a deployment profile is assigned.
6. If the device enrolled but is not compliant, open its compliance report and fix each failing setting, such as BitLocker, the Windows version or Defender.
7. Ask the user to sync from Company Portal and allow up to 30 minutes.

Escalate to Cloud & M365 if several devices fail with the same error, as a policy or tenant setting is the likely cause.""",
    ),
    Article(
        slug="bitlocker-recovery-key",
        title="BitLocker recovery key requested at start up",
        category="Security",
        content="""A BitLocker recovery screen appears after a BIOS or firmware update, a docking station change, a TPM reset or a replaced motherboard. It can also mean someone has tampered with the device, so verify the caller before giving out any key.

1. Verify the user by calling back the number held in the client record. Never read a key to a caller you have not verified.
2. Ask the user for the Key ID shown on the recovery screen. The first eight characters are enough to find the key.
3. In the Intune admin centre or the Microsoft Entra admin centre, open the device, select Recovery keys and match the Key ID.
4. Read the 48 digit recovery key to the user in groups of six and let them enter it.
5. Once Windows starts, finish the pending firmware update properly, or suspend and resume BitLocker, so the prompt does not return.
6. Rotate the recovery key from Intune afterwards, because the old key has now been shared.

If no key is stored for the device, the data cannot be recovered. Escalate to Security Ops if the user did not expect any change to the device.""",
    ),
    Article(
        slug="teams-calling-quality",
        title="Teams calls drop, echo or have poor audio",
        category="Email & M365",
        content="""Poor Teams call quality is usually caused by the local network, a weak Wi-Fi signal, a VPN that sends calls the long way round or a faulty headset. Collect the facts before changing anything.

1. Ask whether the problem affects one person or several, and whether it happens at home, in the office or both.
2. Ask the user to make a test call from Teams settings under Devices, and to report what they hear.
3. Check the headset. Try another USB port, update its firmware and select it as the audio device in Teams.
4. If the user is on VPN, confirm that Teams media traffic is excluded from the tunnel, as Microsoft recommends.
5. Move the user to a wired connection or nearer the access point and compare.
6. In the Teams admin centre, open the user's call analytics to see jitter, packet loss and round trip time for the affected calls.
7. For Teams Phone users who cannot make or receive external calls, check the calling licence and the phone number assignment.

Escalate to Infrastructure L2 if several office users are affected at once, as that points to the internet line or the firewall.""",
    ),
    Article(
        slug="sharepoint-permissions",
        title="SharePoint access denied or wrong permissions",
        category="Email & M365",
        content="""Most SharePoint access problems come from a user who is missing from the right Microsoft 365 group, from broken permission inheritance on a folder, or from a link shared with the wrong person. Only grant access that the site owner has approved.

1. Ask for the exact link, the site name and who approved the access.
2. Open the site, select Settings, then Site permissions, and check whether the user is in the Owners, Members or Visitors group.
3. For a site connected to a team in Teams, add the user to the team instead, so Teams and SharePoint stay in step.
4. If only one folder or file is blocked, open Manage access on it and check whether it has unique permissions.
5. Use Check permissions under Advanced permissions settings to see what the user can actually do.
6. Add people to groups rather than granting rights to individuals, because individual grants are hard to review later.
7. Ask the user to sign out and back in, then try the link again in a private browser window.

If the site owner has left the client, ask the client to name a new owner before changing any permissions.""",
    ),
    Article(
        slug="site-to-site-vpn-down",
        title="Site to site VPN tunnel down between offices",
        category="Network",
        content="""When the tunnel between two offices drops, staff at the branch lose the file server, the finance system and printers at head office. Internet access usually still works, which helps to narrow down the fault.

1. Confirm that both sites have working internet access, from the firewall dashboards or by asking a user at each site to open a public website.
2. On each firewall, open the IPsec status page and note whether phase one or phase two is failing.
3. If phase one fails, check that the public IP address at each end has not changed. Broadband providers sometimes change it after a router swap or an outage.
4. Compare the pre-shared key, the encryption settings and the lifetimes on both ends. They must match exactly.
5. If phase two fails, compare the local and remote subnets configured on both sides.
6. Restart the tunnel from one firewall and watch the log while it connects.
7. Check whether a firmware update or configuration change was made recently, and roll it back if needed.

Treat this as P2 when a whole site cannot work, route it to Infrastructure L2 and update the client every hour.""",
    ),
    Article(
        slug="nas-backup-failed",
        title="NAS or server backup job failed",
        category="Backup & Recovery",
        content="""A failed backup does not stop anyone working, but every night without a good backup raises the risk to the client. Fix it the same day and never close the ticket until a backup has completed.

1. Open the backup console and read the exact error for the failed job and for the previous few runs.
2. Check the free space on the NAS or backup target. A full target is the most common cause.
3. Check that the NAS is powered on, reachable on the network and that every disk reports as healthy.
4. If the job could not log in, check whether the backup service account password expired or was changed.
5. Look for files that were open during the job, such as databases or Outlook data files, and turn on the snapshot option if the software has one.
6. Run the job again by hand and watch it complete.
7. Check that the offsite or cloud copy also ran, as a local copy alone does not protect against fire, theft or ransomware.

If the job reports encrypted or renamed files, or the backups themselves were deleted, stop and escalate to Security Ops straight away.""",
    ),
    Article(
        slug="leaver-offboarding",
        title="Leaver offboarding checklist",
        category="Access & Identity",
        content="""Remove access promptly when someone leaves, ideally at the end of their last working day. A former employee with a working account is a common route for data loss and fraud.

1. Confirm the leaver's name, last day and any instructions from their manager in writing.
2. At the agreed time, block sign in for the user in Microsoft 365 and reset the password.
3. Revoke all sessions and remove their MFA methods so that existing tokens stop working.
4. Remove the user from security groups, distribution lists, shared mailboxes and Teams.
5. Convert the mailbox to a shared mailbox if the manager needs it, and set an automatic reply.
6. Give the manager access to the OneDrive files before the licence is removed, as OneDrive content is deleted after the retention period.
7. Remove the licence once the data is safe, and tell Account Management so billing is updated.
8. Wipe or retire company devices in Intune, collect the hardware and disable VPN, line of business and door access accounts.

Record each step and the time it was done in the ticket, so the client has an audit trail.""",
    ),
    Article(
        slug="email-dns-records",
        title="SPF, DKIM and DMARC records for email delivery",
        category="Email & M365",
        content="""When a client's emails land in spam or are rejected by other companies, the domain often has missing or wrong SPF, DKIM or DMARC records. These DNS records tell other mail servers which senders may use the domain.

1. Ask for an example of a rejected message and the bounce text, which often names the failing check.
2. Look up the domain's TXT records with an online DNS checker or nslookup.
3. Check there is exactly one SPF record. It should include spf.protection.outlook.com and any other service that sends as the domain, such as a CRM or invoicing system.
4. In the Microsoft Defender portal, open the email authentication settings, add the two DKIM CNAME records it shows to DNS and then turn DKIM signing on.
5. Add a DMARC record that starts with a policy of none and a reporting address. Move to quarantine and then reject once the reports show that all genuine mail passes.
6. Allow time for DNS changes to spread, then send a test to an external mailbox and check the message headers.

Only change DNS with the client's approval, and record the old values first so they can be restored.""",
    ),
    Article(
        slug="mfa-fatigue-attack",
        title="Unexpected MFA prompts or MFA fatigue attack",
        category="Security",
        content="""If a user receives MFA approval requests they did not trigger, an attacker almost certainly has their password and is hoping they will tap Approve. Treat this as a security incident even if the user denied every prompt.

1. Tell the user to deny every prompt they did not start, and never to approve one just to make the requests stop.
2. Reset the user's password straight away and revoke all sessions in the Microsoft Entra admin centre.
3. Ask whether they approved any prompt. If they did, assume the account is compromised and follow the phishing response runbook.
4. Review the sign-in logs for the failed and successful attempts, and note the IP addresses and locations.
5. Check for new MFA methods, inbox rules, mail forwarding or app consents added by the attacker.
6. Make sure number matching is on for Microsoft Authenticator, so a prompt cannot be approved without the code shown on screen.
7. Consider a conditional access policy that blocks sign-ins from countries where the client does not operate.

Pass the ticket to Security Ops and keep it at P2 or higher until they confirm the account is safe.""",
    ),
    Article(
        slug="guest-wifi-setup",
        title="Set up or fix guest Wi-Fi",
        category="Network",
        content="""Guest Wi-Fi must be kept apart from the company network, so visitors can browse the internet but cannot reach file servers, printers or staff laptops. Requests usually arrive as a new network for an event, a password change or a report that guests cannot connect.

1. Confirm what the client needs, such as the network name, who receives the password and how often it should change.
2. On the wireless controller, create a separate SSID mapped to the guest VLAN, never to the staff VLAN.
3. Turn on client isolation so that guest devices cannot see each other.
4. Make sure the firewall blocks traffic from the guest VLAN to internal subnets and allows only web traffic and DNS.
5. Set a bandwidth limit per device, so guests cannot slow down the office connection.
6. Use WPA2 or WPA3 with a password that is changed regularly, or a captive portal with terms of use.
7. From a phone on the guest network, check that the internet works and that internal addresses cannot be reached.

If guests connect but get no IP address, check that the DHCP scope for the guest VLAN is not full.""",
    ),
    Article(
        slug="printer-driver-deployment",
        title="Deploy a printer and its driver to laptops",
        category="Hardware",
        content="""Adding printers by hand on each laptop is slow and inconsistent. Deploy them centrally, so every user gets the same driver and settings and a new printer can be rolled out in one change.

1. Collect the printer make, model, IP address and the users or departments who need it.
2. Give the printer a DHCP reservation or a fixed IP address before deploying anything.
3. Download the manufacturer's universal driver from their official support site. Never use drivers from third party download sites.
4. For Intune managed laptops, package the driver with a script that adds the printer port and queue as a Win32 app, or use Universal Print if the client has it.
5. For domain joined laptops with a print server, share the printer from the server and deploy it with Group Policy.
6. Assign the deployment to a small test group first, then to everyone once it works.
7. Since the PrintNightmare security fixes, standard users cannot install most printer drivers themselves. Do not give them admin rights to work around this.

If printing fails after deployment, check the Print Spooler service and the driver version on the laptop.""",
    ),
    Article(
        slug="windows-update-failures",
        title="Windows Update fails to install",
        category="Software",
        content="""Windows updates that fail again and again leave a laptop exposed to known security flaws. Common causes are low disk space, a damaged update cache, a pending restart or an update policy that blocks the update.

1. Note the error code shown in Settings, Windows Update, Update history, such as 0x80070643 or 0x800f0922.
2. Restart the laptop to finish any pending updates, then try again.
3. Make sure at least 20 GB of disk space is free.
4. Run the Windows Update troubleshooter from the Troubleshoot page in Settings.
5. From an admin command prompt, run DISM /Online /Cleanup-Image /RestoreHealth and then sfc /scannow to repair system files.
6. Stop the Windows Update and BITS services, rename the SoftwareDistribution folder and start the services again to reset the update cache.
7. In Intune, check that the update ring assigned to the device has not paused or deferred the update.
8. For one stubborn update, download it from the Microsoft Update Catalog and install it by hand.

Check the Windows release health page for known issues, and escalate to Infrastructure L2 if many devices fail on the same update.""",
    ),
    Article(
        slug="onedrive-sync-issues",
        title="OneDrive not syncing or showing sync errors",
        category="Email & M365",
        content="""OneDrive sync problems show as a red cross, a paused icon or files that never appear on other devices. The files are usually safe online, so check there before changing anything on the laptop.

1. Ask the user to open OneDrive on the web and confirm the files are there and up to date.
2. Select the OneDrive cloud icon next to the clock and read the error message.
3. Check that the user is signed in with their work account and that syncing is not paused.
4. Look for file names with characters OneDrive does not allow, or paths longer than 400 characters, and rename them.
5. Check the free disk space. Turn on Files On Demand so files only download when they are opened.
6. Make sure the files are not open in another app, as locked files will not sync.
7. If sync is still stuck, reset OneDrive by running onedrive.exe /reset from its program folder, then start OneDrive again.

Never delete the local OneDrive folder to fix sync, because unsynced changes would be lost. Escalate to Cloud & M365 if a whole SharePoint library will not sync for several users.""",
    ),
    Article(
        slug="office-activation-errors",
        title="Microsoft 365 apps show Unlicensed Product or activation errors",
        category="Software",
        content="""When Word, Excel or Outlook shows Unlicensed Product, Product Deactivated or loops at sign in, the apps cannot confirm the licence. The user can usually still open files but cannot edit them.

1. In the Microsoft 365 admin centre, check that the user has a licence that includes the desktop apps, such as Business Standard or Business Premium.
2. Check how many devices the user has activated. Each user can install the apps on up to five PCs or Macs, so sign out of old devices if they have reached the limit.
3. In any Office app, open File, then Account, and check which account is signed in. Sign out of personal or old accounts.
4. Close every Office app, then sign in again with the work account.
5. Make sure the laptop date, time and time zone are correct, as a wrong clock breaks activation.
6. If the problem remains, run an online repair of Microsoft 365 from Installed apps.
7. On shared or multi user computers, check that shared computer activation is turned on.

If the licence was removed by mistake during a billing change, pass the ticket to Account Management so the subscription is corrected.""",
    ),
    Article(
        slug="mailbox-full-archive",
        title="Mailbox full or nearly full",
        category="Email & M365",
        content="""When a Microsoft 365 mailbox reaches its quota, the user first gets warnings, then cannot send and finally cannot receive email. Business Basic, Standard and Premium mailboxes hold 50 GB, and Microsoft 365 E3 or Exchange Online Plan 2 mailboxes hold 100 GB.

1. In the Exchange admin centre or in Outlook, check the mailbox size and its quota.
2. Ask the user to empty Deleted Items and Junk Email, then sort by size to find large attachments they no longer need.
3. Turn on the online archive for the mailbox. Business Basic and Standard include a 50 GB archive, and Business Premium includes Exchange Online Archiving, which can grow much larger.
4. Apply a retention policy that moves items older than an agreed age, such as two years, into the archive automatically.
5. Explain to the user that archived mail stays searchable in Outlook under Online Archive.
6. Check for large shared calendars or old PST imports that inflate the size.
7. If the mailbox is still close to its quota with an archive in place, discuss a larger plan with Account Management.

Never delete a user's mail on their behalf without written approval.""",
    ),
    Article(
        slug="internet-outage-office",
        title="Whole office has no internet",
        category="Network",
        content="""When everyone in an office loses internet at once, the fault is almost always the broadband line, the router or the firewall rather than the laptops. Work from the outside in.

1. Check the internet provider's service status page and the MSP monitoring dashboard for alerts on the site.
2. Ask someone on site whether the router and the firewall have power, and which lights are on or flashing.
3. Ask them to restart the router, wait two minutes, then restart the firewall.
4. If the firewall is reachable remotely, log in and check whether the WAN interface has an IP address and a gateway.
5. If the firewall shows the line as down, raise a fault with the provider, quoting the circuit reference from the client record.
6. If a backup 4G or 5G connection is fitted, confirm it has taken over and tell the client which services may be slower.
7. Suggest that staff work from home or use mobile hotspots while the line is down, if the client allows it.

A whole site without internet is P2 for Infrastructure L2. Update the client at least every hour and record the provider's fault reference in the ticket.""",
    ),
    Article(
        slug="account-lockout-repeated",
        title="Account keeps locking out",
        category="Access & Identity",
        content="""An account that locks again soon after being unlocked is usually caused by an old password saved somewhere, such as a phone, a mapped drive or a scheduled task. It can also be a password spraying attack, so look at where the attempts come from.

1. Unlock the account in Active Directory, or check the Microsoft Entra sign-in logs for a cloud only user.
2. Find the source of the failed attempts. On a domain controller, event ID 4740 names the computer that caused the lockout.
3. Ask the user to update or remove the saved password in their phone mail app, on any second laptop and in Windows Credential Manager.
4. Check for mapped drives, scheduled tasks or services that run under the user's account with an old password.
5. Sign out old Remote Desktop sessions that the user left open on a server.
6. If the attempts come from unknown IP addresses or other countries, treat this as an attack, reset the password and pass the ticket to Security Ops.
7. Confirm the account stays unlocked for at least an hour before closing the ticket.

Remind the user never to share their password, even with colleagues.""",
    ),
    Article(
        slug="licence-changes-billing",
        title="Add, remove or change Microsoft 365 licences",
        category="Billing & Account",
        content="""Licence changes alter the client's monthly invoice, so an authorised contact must approve them and Account Management must be told. Many plans are bought on an annual commitment, which limits when licences can be reduced.

1. Check the request comes from a contact named in the client record as allowed to approve spend.
2. Confirm the user, the licence type and the date the change should take effect.
3. For a new starter, check whether a spare licence already exists before buying another.
4. Assign or remove the licence in the Microsoft 365 admin centre, then check that the user's apps and mailbox still work.
5. For a reduction on an annual commitment, explain that the seat count can usually only be lowered at renewal, and record the request against that date.
6. Before removing a leaver's licence, make sure their mailbox and OneDrive data have been kept, as set out in the leaver checklist.
7. Pass the change to Account Management with the ticket reference, so the next invoice is correct.

For a dispute about an invoice amount, do not change any licences. Pass the ticket to Account Management to review.""",
    ),
)


def _seed_missing(
    conn: Connection, time_budget: float, clock: Callable[[], float]
) -> tuple[int, rag.EmbeddingError | None, int]:
    """Returns (articles inserted, the embedding error that stopped the run, articles embedded)."""
    failure: rag.EmbeddingError | None = None
    with conn.transaction():
        if not store.try_advisory_xact_lock(conn, KB_SEED_LOCK_KEY):
            return 0, None, 0
        existing = store.existing_kb_slugs(conn)
        missing = [article for article in KB_ARTICLES if article.slug not in existing]
        if not missing:
            return 0, None, 0
        deadline = clock() + time_budget
        embedded: list[tuple[Article, list[float]]] = []
        for start in range(0, len(missing), SEED_BATCH_SIZE):
            if embedded and clock() >= deadline:
                logger.info("knowledge base seeding paused after %d articles, the rest follow on the next run", len(embedded))
                break
            batch = missing[start:start + SEED_BATCH_SIZE]
            try:
                vectors = rag.embed([article.embedding_text() for article in batch], timeout=SEED_EMBED_TIMEOUT_SECONDS)
            except rag.EmbeddingError as exc:
                failure = exc
                break
            embedded.extend(zip(batch, vectors, strict=True))
        rows = [
            (article.slug, article.title, article.category, article.content, rag.vector_literal(vector))
            for article, vector in embedded
        ]
        inserted = store.insert_kb_articles(conn, rows) if rows else 0
    return inserted, failure, len(embedded)


def seed_kb(
    conn: Connection,
    *,
    time_budget: float = SEED_TIME_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Embed and insert any missing articles. Safe to call from concurrent instances.

    The transaction scoped advisory lock lets one instance seed while the
    others return at once, and ``ON CONFLICT (slug) DO NOTHING`` makes a
    repeated run harmless. Each embedding call carries at most
    ``SEED_BATCH_SIZE`` texts, because the edge runtime caps CPU time per request.

    A run stops starting new batches once ``time_budget`` seconds have passed and
    inserts what it has, so a health check never waits on every new article at
    once. The next call carries on from the articles still missing. If a batch
    fails, the batches before it are still inserted. ``EmbeddingError`` is raised
    only when nothing could be embedded. Returns the number of articles inserted.
    """
    inserted, failure, embedded = _seed_missing(conn, time_budget, clock)
    if failure is not None:
        if not embedded:
            raise failure
        logger.warning("knowledge base seeding stopped early after %d articles: %s", embedded, failure)
    return inserted


def seed_if_due(conn: Connection, *, clock: Callable[[], float] = time.monotonic) -> int:
    """Seeding entry point for the health check, with a cooldown after a failed run.

    The dashboard polls health every minute, so without the cooldown an article
    that cannot be embedded would cost a slow request on every poll.
    """
    global _cooldown_until
    if clock() < _cooldown_until:
        return 0
    inserted, failure, embedded = _seed_missing(conn, SEED_TIME_BUDGET_SECONDS, clock)
    if failure is not None:
        _cooldown_until = clock() + FAILURE_COOLDOWN_SECONDS
        if not embedded:
            raise failure
        logger.warning("knowledge base seeding stopped early after %d articles: %s", embedded, failure)
    return inserted


def reset_cooldown() -> None:
    global _cooldown_until
    _cooldown_until = 0.0
