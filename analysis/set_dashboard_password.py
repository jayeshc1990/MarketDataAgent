"""Set the dashboard login (one username, two passwords). Stores only salted hashes in .env.

    .venv/bin/python analysis/set_dashboard_password.py
    sudo systemctl restart marketdataui       # pick up the new login
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.auth import PASS1_KEY, PASS2_KEY, PROJECT_ROOT, USER_KEY, hash_password  # noqa: E402

MIN_LENGTH = 10


def ask_password(label: str) -> str:
    while True:
        pw = getpass.getpass(f"{label} (min {MIN_LENGTH} characters): ")
        if len(pw) < MIN_LENGTH:
            print("  too short")
        elif getpass.getpass(f"{label} again: ") != pw:
            print("  did not match")
        else:
            return pw


def update_env(path: Path, values: dict):
    """Replace these keys in .env (or append them), leaving every other line as it is."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values:
            if key not in done:
                out.append(f"{key}={values[key]}")
                done.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)


def main():
    user = input("Username: ").strip()
    if not user:
        sys.exit("Username is required.")
    pw1 = ask_password("Password 1")
    pw2 = ask_password("Password 2")
    if pw1 == pw2:
        sys.exit("The two passwords must be different.")
    env = PROJECT_ROOT / ".env"
    update_env(env, {USER_KEY: user, PASS1_KEY: hash_password(pw1), PASS2_KEY: hash_password(pw2)})
    print(f"Saved to {env}. Restart the dashboard: sudo systemctl restart marketdataui")


if __name__ == "__main__":
    main()
