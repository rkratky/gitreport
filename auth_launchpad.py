# This script is used for a one-time authentication with Launchpad.
# Running this script will open a web browser and ask you to authorize
# the "gitreport-cli" application. Once authorized, it will save your
# credentials to `~/.launchpadlib/creds` for future use by the main tool.

from launchpadlib.launchpad import Launchpad

import os
from pathlib import Path
from launchpadlib.launchpad import Launchpad

LP_CREDENTIALS_PATH = Path(os.path.expanduser("~/.config/gitreport/lp_credentials"))

print("Attempting to authenticate with Launchpad...")
print(f"Credentials will be saved to: {LP_CREDENTIALS_PATH}")
print("Your web browser should open. Please authorize the application.")

try:
    # Ensure the directory exists
    LP_CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)

    launchpad = Launchpad.login_with(
        "gitreport-cli",
        "production",
        credentials_file=str(LP_CREDENTIALS_PATH),
        version="devel",
    )
    print("\nAuthentication successful!")
    print("Credentials have been saved for future use.")
except Exception as e:
    print(f"\nAn error occurred during authentication: {e}")
    print("Please ensure you have an internet connection and try again.")

