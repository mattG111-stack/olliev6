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


settings = Settings()
