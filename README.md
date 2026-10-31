# Akash Study Forum

## Run the AI tutor locally

The page and AI API are served by the Python backend. The provider API key is read
only by the server; do not add it to `index.html` or commit it to source control.

1. Install Python 3.10 or newer.
2. Install the backend packages:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Set the AI provider settings in PowerShell. Use the provider's OpenAI-compatible
   API base URL (including `/v1` when required), the exact model name, and an API
   key supplied by that provider:

   ```powershell
   $env:AI_BASE_URL = "https://your-provider.example/v1"
   $env:AI_MODEL = "your-model-name"
   $env:AI_API_KEY = "your-secret-key"
   ```

4. Start the web app:

   ```powershell
   python -m uvicorn app:app --host 127.0.0.1 --port 8000
   ```

5. Open <http://127.0.0.1:8000>.

The provider endpoint must implement the OpenAI-compatible `POST
/chat/completions` format. `AI_API_KEY` is never sent to the browser. For
production, configure these values in the host's secret/environment settings and
serve the site over HTTPS. The built-in per-IP rate limit is process-local; use a
shared rate limiter or API gateway when running multiple workers or instances.

## Paid Study Hub tests

Paid tests, student orders, payment references, and one-time access codes are
stored in the server-side SQLite database (`asf_study.sqlite3` by default). The
paid checkout requires the page to be opened through this FastAPI server, not as
a local `file://` page.

Set a one-time CRM owner-setup password and a separate Study Hub password
before starting the server. CRM staff sign in with named email/password accounts;
the owner setup password is not used for normal CRM logins.

```powershell
$env:CRM_ADMIN_PASSWORD = "use-a-different-long-private-password"
$env:STUDY_ADMIN_PASSWORD = "use-a-long-private-password"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open the site at <http://127.0.0.1:8000>. In the CRM portal, choose **Create
owner account** and enter the one-time `CRM_ADMIN_PASSWORD`, the owner's name
and email, and a new private password of at least 12 characters. The first
owner account can only be created once. After setup, `CRM_ADMIN_PASSWORD` is
no longer used for CRM logins and can be removed from the server environment.
The owner can add/remove named staff accounts under **Staff Accounts**. Do not
allow public self-registration.

The **Forgot password?** action sends a one-time, 30-minute reset link by email;
it never sends a password. Configure an HTTPS public site URL and SMTP sender
before enabling recovery. For example, set these server-side environment
variables (use the SMTP provider's app password where applicable):

```text
CRM_PUBLIC_URL=https://your-site.example
CRM_SMTP_HOST=smtp.your-provider.example
CRM_SMTP_PORT=587
CRM_SMTP_USER=your-smtp-user
CRM_SMTP_PASSWORD=your-smtp-app-password
CRM_EMAIL_FROM=ASF CRM <no-reply@your-site.example>
```

Until SMTP is configured, password recovery displays an explicit message and
the CRM owner must remove and recreate the staff account with a new temporary
password under **Staff Accounts**. In the
**Paid Test Orders** section, sign in separately with `STUDY_ADMIN_PASSWORD`.
The Study Hub manager publishes paid tests to the server; each paid test needs
its actual test URL and a price from ₹9 to ₹99. Previously browser-only paid
tests can be selected under **Publish on server**, reviewed, and migrated.

CRM enquiries, counselor notes, lead statuses, and YouTube video settings are
stored in the server-side SQLite database and shared by staff using the same
server. On the first successful CRM login from a browser, its existing lead and
video data is imported to the server and removed from that browser's local
storage after import succeeds. Verify the imported records in the CRM and
export a CSV backup; back up the SQLite database regularly. CRM staff can
delete individual enquiries from the lead list after typing `DELETE` in the
confirmation prompt. Check retention and record-keeping requirements first;
backup copies are not removed by this action.

The authenticated CRM workspace uses a responsive sidebar for Overview,
Enquiries, Fee Plans, Video Manager, and Study Hub; selecting a section shows
only that workspace panel.

CRM staff can create, edit, hide, or delete fee plans under **Fee Plans &
Discounts**. Board suggestions include UP Board, Bihar Board, CBSE, and ICSE;
class suggestions include Classes 8–12; course suggestions include school /
board preparation, JEE, NEET, Foundation (Basic), JEE Foundation, NEET
Foundation, and Olympiad. These are suggestions, and staff can enter other
ASF offerings. Each plan is specific to one board, class, course, subject
combination, and billing period (monthly, per term/installment, or full
academic year). Subject suggestions adapt to the selected course and include
Maths, Science, Physics, Chemistry, Biology, English Literature, English
Grammar, and foundation/school subjects. Add both English subjects to one plan
for a combined fee, or create separate plans for separate fees; the same
applies to Physics, Chemistry, and Biology bundles. For Foundation (Basic),
create approved basic-oriented single-subject and core bundles as separate
plans. Enter only ASF-approved prices. An optional offer amount must be lower
than the regular fee and can include a discount label; mark plans as
recommended when appropriate. The public Fees page lists active plans and
links visitors to ASF for eligibility, batch, installment, or concession
questions. When a visitor selects subjects and a matching bundle is available,
the page invites them to compare it with separately listed ASF plans. It shows
bundle savings only when a same-board, class, course, and fee-period plan exists
for every subject; the comparison uses each plan's current published offer (if
any). Otherwise, no savings claim is shown. No market-based prices, urgency,
or popularity claims are invented.

## Back up the database

Before collecting real enquiries, create a database backup:

```powershell
python backup_db.py
```

By default, the backup is written to `~/ASF-Backups`. The script uses
`STUDY_DB_PATH` when configured, otherwise it backs up `asf_study.sqlite3` in
the project folder. To select a different source or destination:

```powershell
python backup_db.py --database "C:\path\to\asf_study.sqlite3" --destination "D:\ASF-Backups"
```

The script uses SQLite's online backup API and checks the resulting database
integrity. Run it regularly and keep a copy on a different drive or secure
storage; a backup on the same server will not protect against server loss.

### Schedule daily backups on Windows

From PowerShell in the project folder, install a current-user Windows Task
Scheduler task:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_backup_task.ps1
```

By default, the task runs daily at 2:00 AM, creates an integrity-checked
backup in `~/ASF-Backups`, and removes only ASF backup files older than 30
days, and only after the new backup succeeds. It uses `.venv\Scripts\python.exe`
when available and otherwise uses `python.exe`. The database path is taken
from `STUDY_DB_PATH` when set at installation; otherwise it uses
`asf_study.sqlite3` in the project folder. If the project or database path
changes, install the task again.

The computer must be on and the Windows account signed in for the task to run.
If the scheduled time is missed, Windows starts it when it is next available.
This backup folder is on the same computer by default, so periodically copy
backups to a different drive or secure storage. Check **Task Scheduler** for
the task's last run result, and check the backup folder for recent files.

To remove the scheduled task:

```powershell
Unregister-ScheduledTask -TaskName 'ASF Daily SQLite Backup' -Confirm:$false
```

## Privacy notice

The website includes a draft Privacy Notice linked from its footer and
data-submission forms. It documents the current form, CRM, Study Hub, browser
storage, and AI-provider behavior. Before public launch, ASF must verify the
AI/service-provider and organizational data-sharing practices, when the
retention periods start, and any legal requirements; the draft is not legal
advice. ASF's stated targets are up to 12 months for enquiries and 1 month for
paid orders. Deletion is manual, not automatic. A Study Hub admin can
permanently delete a paid order and its access code from the order list after
confirmation; check the retention period and record-keeping requirements
first. Backups need separate rotation and may retain deleted data.

Student payment flow is currently manual: the site creates an order, WhatsApp
is used to request the center's UPI details, the student submits the UTR, staff
checks the payment in the UPI/bank account, then approves the order and sends
the generated one-time access code over WhatsApp. The student redeems that code
on the checkout page to receive the test link. If an unused code is lost, staff
can replace it from the order list; a redeemed code cannot be replaced. The
disabled Google Pay button is only a placeholder; Razorpay and automatic payment
verification are not configured. Never approve an order based only on the
student's submitted UTR.

For a public launch, set both admin passwords in the hosting provider's secret
settings, use HTTPS, restrict staff access, and back up the SQLite database.
Configure the AI settings only if the AI tutor is included in the launch. Do
not use the local development server as a public production host.

### Prepare a Render staging deployment

The root `render.yaml` defines one Python web service, a persistent disk for
SQLite, and two passwords that Render prompts you to enter as secrets. It does
not configure AI tutor credentials; the tutor is optional and is not part of
this launch setup. The persistent disk requires a paid Render web-service
instance, and the service must remain a single instance while using this
SQLite disk.

After the project is reviewed and pushed to GitHub, create a Render Blueprint
from the repository and enter long, unique values for the one-time
`CRM_ADMIN_PASSWORD` owner setup and `STUDY_ADMIN_PASSWORD` in Render's secret
prompt. Do not enter either password in `render.yaml` or commit them to the
repository. Configure SMTP and `CRM_PUBLIC_URL` in Render's environment settings
to enable reset emails. Remove the CRM bootstrap secret after the owner account
has been created. Confirm the
Blueprint's plan and storage pricing in Render before creating paid resources.
The deployment uses a temporary `onrender.com` address; buying a custom domain
is not required for staging.

The persistent disk preserves the database across deploys and restarts, but it
is not a substitute for off-site backups. Configure and test backups for the
hosted database separately before public launch. The Windows Task Scheduler
task in this project only backs up the local Windows database.

Before putting this project in GitHub, review `.gitignore` and confirm that
local databases, backups, virtual environments, and secret files are excluded.
Never commit `.env` files, production credentials, or real student/customer
records. The checked-in `.gitignore` does not remove files already tracked by
Git.
