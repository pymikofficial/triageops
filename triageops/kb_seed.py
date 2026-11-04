"""The service desk knowledge base: twelve MSP runbook articles and an idempotent seeder.

Articles are written for both audiences that read them. Engineers follow the
steps, and the triage model quotes safe customer steps in its draft replies.
"""

from dataclasses import dataclass

from . import rag, store
from .db import Connection

# Any constant works. It only has to be the same for every instance.
KB_SEED_LOCK_KEY = 7_401_000_012
SEED_EMBED_TIMEOUT_SECONDS = 25.0


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
    ),
)


def seed_kb(conn: Connection) -> int:
    """Embed and insert any missing articles. Safe to call from concurrent instances.

    The transaction scoped advisory lock lets one instance seed while the
    others return at once, and ``ON CONFLICT (slug) DO NOTHING`` makes a
    repeated run harmless. Returns the number of articles inserted.
    """
    with conn.transaction():
        if not store.try_advisory_xact_lock(conn, KB_SEED_LOCK_KEY):
            return 0
        existing = store.existing_kb_slugs(conn)
        missing = [article for article in KB_ARTICLES if article.slug not in existing]
        if not missing:
            return 0
        vectors = rag.embed([article.embedding_text() for article in missing], timeout=SEED_EMBED_TIMEOUT_SECONDS)
        rows = [
            (article.slug, article.title, article.category, article.content, rag.vector_literal(vector))
            for article, vector in zip(missing, vectors, strict=True)
        ]
        return store.insert_kb_articles(conn, rows)
