"""Twenty realistic ticket texts and the runbook article that should answer each one."""

RETRIEVAL_CASES: tuple[tuple[str, str], ...] = (
    ("Laptop is asking for a BitLocker recovery key after the BIOS update this morning", "bitlocker-recovery-key"),
    ("Company Portal says my new laptop cannot enrol, error 80180014", "intune-enrolment-fails"),
    ("People say my voice keeps breaking up on Teams calls and there is an echo", "teams-calling-quality"),
    ("I get access denied when I open the finance site in SharePoint", "sharepoint-permissions"),
    ("Bristol branch cannot reach the file server at head office since last night, the internet works fine", "site-to-site-vpn-down"),
    ("Last night's backup job to the Synology NAS failed with an error", "nas-backup-failed"),
    ("Sarah leaves on Friday, please disable her account and give her manager access to her mailbox", "leaver-offboarding"),
    ("Our emails to customers keep going to spam, do we need DMARC or SPF?", "email-dns-records"),
    ("I keep getting Authenticator approval requests on my phone that I did not ask for", "mfa-fatigue-attack"),
    ("We need a separate wifi network for visitors in reception", "guest-wifi-setup"),
    ("Please add the new Ricoh printer and driver to every laptop in accounts", "printer-driver-deployment"),
    ("Windows update keeps failing with 0x80070643 and rolling back", "windows-update-failures"),
    ("OneDrive has a red cross and my files are not syncing to my other PC", "onedrive-sync-issues"),
    ("Excel says unlicensed product and I cannot edit anything", "office-activation-errors"),
    ("Outlook says my mailbox is full and I cannot send email", "mailbox-full-archive"),
    ("Nobody in the office has internet, the router lights are flashing orange", "internet-outage-office"),
    ("My account keeps getting locked every few minutes even after a password reset", "account-lockout-repeated"),
    ("We need two more Business Premium licences for the sales team, how does billing change", "licence-changes-billing"),
    ("VPN error 809 when I try to connect from the hotel", "vpn-error-809"),
    ("Someone opened an attachment, every file now ends in .locked and there is a ransom note", "ransomware-first-response"),
)
