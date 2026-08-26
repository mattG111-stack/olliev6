from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    # Twelve hours, not one. Nothing refreshes a token, so this is the whole
    # session: at 60 minutes anyone who left a tab open over lunch came back to
    # a sign-in page, and every background poll after the hour mark logged a
    # 401. A property tool people research in across an afternoon needs a
    # working day, not an hour. Override with JWT_EXPIRY_MINUTES.
    jwt_expiry_minutes: int = 720
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str = "changeme"
    cors_origins: str = "http://localhost:3000"
    batch_retention_limit: int = 12  # keep last N batches per type+region
    brave_api_key: str = ""          # optional — reliable search for external estimates
    # Apify runs the headless browser that Trade Me, OneRoof and realestate.co.nz
    # need to render their figures. Blank = those three sources are skipped and
    # the button still works with the two that can be read directly.
    apify_token: str = ""
    # Ask the portals once a day about anything new, unattended. Off by default:
    # a job that reaches the internet and spends money should be switched on
    # deliberately, not started because a deploy went out.
    portals_daily: bool = False
    stripe_secret_key: str = ""      # optional — Stripe billing metrics on the admin dashboard
    # Toitū Te Whenua LINZ Data Service key — legal parcel boundaries for the
    # Sun & shade panel. Free to obtain. Without it the panel falls back to a box
    # sized from the listing's land area, so this is optional, not required.
    linz_api_key: str = ""

    # --- self-serve onboarding + trial billing ---------------------------------
    app_base_url: str = "http://localhost:3000"   # where Stripe redirects back after checkout
    trial_days: int = 7                            # free-trial length; first charge after this
    # Stripe subscription price the trial converts to. Create a recurring Price in
    # Stripe and put its id here; without it, checkout can't start.
    stripe_price_id: str = ""
    stripe_webhook_secret: str = ""                # verifies Stripe webhook signatures
    # Email delivery (Resend). Without a key we log the code to the server instead
    # of sending — the flow still works end-to-end in dev.
    resend_api_key: str = ""
    email_from: str = "Apex Property <onboarding@resend.dev>"
    # SMS delivery (Twilio) — not wired yet; phone codes are logged for now.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    verify_code_ttl_minutes: int = 15              # how long an email/phone code stays valid

    @property
    def cors_origin_list(self) -> list[str]:
        return [s.strip() for s in self.cors_origins.split(",") if s.strip()]


def _load() -> Settings:
    """Settings, or one plain sentence saying what is missing.

    A missing environment variable used to end a deploy like this:

        pydantic_core._pydantic_core.ValidationError: 2 validation errors for
        Settings
        database_url
        jwt_secret
          Field required [type=missing, input_value={}, input_type=dict]

    ...repeated for every restart, 380 lines of it, to say "two variables are
    not set". The process cannot start without them and should not pretend
    otherwise — but the person reading the log at 2am should be told which two
    in the first line, not the fortieth.
    """
    try:
        return Settings()
    except ValidationError as e:
        missing = sorted({str(err["loc"][0]).upper() for err in e.errors()
                          if err.get("type") == "missing"})
        if not missing:
            raise
        print(
            "\n"
            "  CANNOT START — these environment variables are not set:\n"
            + "".join(f"    {name}\n" for name in missing)
            + "\n"
            "  DATABASE_URL is the Postgres connection string; on Railway it is\n"
            "  usually a reference to the database service. JWT_SECRET is any long\n"
            "  random string — changing it signs everyone out, so reuse the old one.\n"
            "\n"
            "  Nothing else is wrong: the build succeeded and the code is fine.\n",
            flush=True)
        raise SystemExit(1) from None


settings = _load()
