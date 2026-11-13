# SmartPark

SmartPark is a Django-template parking reservation platform with PostgreSQL, Django REST Framework/JWT, Channels, QR tickets, operator workflows, and Razorpay test-mode checkout. The UI is server-rendered; it does not use a JavaScript application framework.

## Requirements

- Python 3.10+
- PostgreSQL 13+ (optional for local development; SQLite is supported locally)
- Redis for production WebSocket fan-out (development uses an in-memory channel layer)

## Local setup

1. Create and activate a virtual environment, then install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set a unique `DJANGO_SECRET_KEY`. For local development without PostgreSQL, set `DJANGO_DB_ENGINE=sqlite`; the database is stored in `db.sqlite3`. Otherwise, create a PostgreSQL database and user and configure the `POSTGRES_*` values. To enable checkout, set `RAZORPAY_TEST_MODE=True`, then enter the matching Razorpay **Test Mode** key ID and secret in `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`; restart Django after changing `.env`. Never share the API secret or use live credentials for local testing.

   The payment page also offers a server-to-server status check to recover a captured payment after the browser callback was lost. For automatic recovery when customers do not return, configure a Razorpay Test Mode webhook for `payment.authorized`, `payment.captured`, `payment.failed`, and `order.paid` at `/payments/razorpay/webhook/`. Set the webhook's signing secret (not the API key secret) as `RAZORPAY_WEBHOOK_SECRET`. Razorpay must be able to reach the configured URL; a local-only `localhost` server is not externally reachable.

   Prefer enabling automatic capture in the Razorpay Dashboard. SmartPark defaults `RAZORPAY_CAPTURE_AUTHORIZED_PAYMENTS` to `True` in Test Mode (and `False` in Live Mode), so a server-verified authorized test payment is captured and fetched again before the booking is confirmed. Set it to `False` when capture is managed elsewhere; an authorized-but-uncaptured payment is never treated as paid. In Live Mode, explicitly set it to `True` only if you intend SmartPark to capture authorized payments.

3. Apply the included migrations, then create the first administrator:

   ```powershell
   py manage.py migrate
   py manage.py seed_demo
   py manage.py createsuperuser
   py manage.py runserver
   ```

   Daphne is installed and enabled in Django so `runserver` serves both HTTP and WebSocket traffic through the ASGI application. For production, configure the application server to load `smartpark.asgi:application`, not `smartpark.wsgi:application`.

   The demo command adds a sample garage in Pune so city search returns a result. Search only finds active locations already added to the database; add operators and additional locations, floors, slots, and vehicles through `/admin/`. Customers can register at `/accounts/register/`.

4. Open `http://127.0.0.1:8000/`. Admin tools are at `/admin/`; the customer and operator dashboards are at `/dashboard/`.

For production, set `DJANGO_DEBUG=False`, use a secret manager for credentials, configure HTTPS and trusted hosts, run Redis, and serve collected static/media files through a production web server. The console email backend is intended for local development; configure an SMTP backend before sending real email.

## Features

- Customer registration, login, profile, vehicle ownership, searchable location listings, slot selection, bookings, QR tickets, and booking history.
- Operator dashboard, scoped QR ticket verification, and vehicle entry/exit tracking with final fee calculation.
- Admin analytics and Django admin CRUD for users, locations, floors, slots, vehicles, reservations, payments, tickets, sessions, and notifications.
- Reservation overlap checking inside a database transaction while locking the selected slot; availability-change events are broadcast over Channels.
- Razorpay Test Mode checkout with server-side signature and captured-amount verification, payment-status reconciliation, retryable failed attempts, optional authorized-payment capture, and an idempotent signed webhook.
- Booking confirmation creates one detailed QR ticket and a downloadable PDF; failed or cancelled checkout never confirms a reservation.
- REST endpoints use JWT or Django session authentication, serializers, filtering, pagination, and role-aware ownership checks.

## REST endpoints

JWT tokens: `POST /api/auth/login/` (then use `Authorization: Bearer <access>`); refresh at `/api/auth/token/refresh/`.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/auth/register/` | Create customer account |
| GET/PATCH | `/api/auth/profile/` | Current profile |
| GET | `/api/parking/locations/` | Search parking locations |
| GET | `/api/parking/slots/` | Filter parking slots |
| GET | `/api/bookings/` | Current user's reservations |
| POST | `/api/bookings/create/` | Reserve a slot (`slot_id`, `vehicle_id`, `starts_at`, `ends_at`) |
| POST | `/api/bookings/cancel/` | Cancel a future confirmed booking (`booking_id`) |
| POST | `/api/payments/create-order/` | Create a Razorpay order (`booking_id`) |
| POST | `/api/payments/verify/` | Verify Razorpay callback payload |
| POST | `/payments/razorpay/webhook/` | Verify Razorpay webhook signature and recover payment updates |
| POST | `/api/qr/verify/` | Verify ticket token (operator/admin) |
| POST | `/api/parking/entry/`, `/api/parking/exit/` | Record vehicle movement (`token`) |
| GET | `/api/admin/analytics/` | Admin analytics |

WebSocket availability notifications are available at `/ws/availability/`.

## Notes

- Reservations quote a minimum of one hour, rounded up to the next whole hour.
- The contact form uses Django's configured email backend. Configure SMTP for delivery outside local development.
- A slot is considered held while its reservation is pending payment. Add a scheduled cleanup policy for abandoned pending reservations before production launch.
- Run `py manage.py check` after configuring the environment. To run the included automated tests without a local PostgreSQL service, set `$env:DJANGO_DB_ENGINE = "sqlite"` in PowerShell before `py manage.py test`; unset the variable to use PostgreSQL again.
