from pydantic_settings import BaseSettings
from typing import Optional
import os
from zoneinfo import ZoneInfo


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "RTL-SDR Monitor"
    APP_ENV: str = "development"
    DEBUG: bool = True
    SECRET_KEY: str = "change-this-to-a-very-long-random-secret-key"
    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 8000

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/rtl_sdr.db"
    DATABASE_URL_SYNC: str = "sqlite:///./data/rtl_sdr.db"

    # JWT Auth
    JWT_SECRET_KEY: str = "change-this-to-another-very-long-random-key"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # RTL-TCP Connection
    RTL_TCP_HOST: str = "127.0.0.1"
    RTL_TCP_PORT: int = 1234
    RTL_TCP_SAMPLE_RATE: int = 2048000  # 2.048 MSPS (most stable for RTL-SDR)
    RTL_TCP_CENTER_FREQ: int = 145500000
    RTL_TCP_GAIN: int = 30
    RTL_TCP_FREQ_CORRECTION_PPM: int = 0  # Parts per million frequency correction

    # Audio Settings
    # Audio output sample rate.
    # 32000 Hz chosen to support WBFM broadcast audio (up to 15 kHz music)
    # while keeping bandwidth modest for live streaming.
    #   Nyquist = 16 kHz → covers 30-15000 Hz WBFM audio band fully.
    #   NBFM voice (300-3400 Hz) also works fine at this rate.
    AUDIO_SAMPLE_RATE: int = 32000
    AUDIO_CHANNELS: int = 1
    SQUELCH_LEVEL: float = -70.0
    VAD_AGGRESSIVENESS: int = 3
    VAD_SILENCE_TIMEOUT: int = 3

    # Speech-to-Text
    STT_PROVIDER: str = "speechmatics"  # speechmatics | whisper
    SPEECHMATICS_API_KEY: str = ""
    SPEECHMATICS_LANGUAGE: str = "th"
    WHISPER_MODEL_SIZE: str = "base"
    WHISPER_LANGUAGE: str = "th"
    # Azure Cognitive Services Speech
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_REGION: str = "southeastasia"
    AZURE_SPEECH_LANGUAGE: str = "th-TH"

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ENABLED: bool = False

    # Timezone Settings
    TIMEZONE: str = "Asia/Bangkok"  # Default timezone for display
    
    # Recordings
    RECORDINGS_DIR: str = "./frontend/assets/audio"
    MAX_RECORDING_DURATION: int = 300
    AUTO_PLAY_NEW: bool = True

    # Auto-start streaming on server startup (background 24/7 operation)
    AUTO_START_STREAMING: bool = True
    AUTO_START_RETRY_INTERVAL: int = 10  # seconds between retry if RTL-TCP offline
    AUTO_START_MAX_RETRIES: int = 0  # 0 = unlimited

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }


settings = Settings()