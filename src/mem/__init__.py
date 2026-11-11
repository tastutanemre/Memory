"""mem - sifir bagimlilikli, kurumsal DLP dostu Ikinci Beyin (muhendislik hafizasi) motoru."""

__version__ = "0.1.0"
SCHEMA_VERSION = 1
SERVER_NAME = "mem"

TRUST_VALUES = ("trusted", "untrusted")
STATUS_VALUES = ("active", "superseded", "archived", "draft")
KIND_VALUES = (
    "overview",
    "rule",
    "decision",
    "threads",
    "session",
    "knowledge",
    "receipt",
    "note",
)
