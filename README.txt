Schoology PDF Downloader v5.5
=============================

WINDOWS - FIRST RUN
-------------------
1. Install Python 3.10 or newer from python.org if it is not already installed. Enable the
   "Add Python to PATH" option during installation.
2. Double-click install_and_run.bat.
3. Enter the Schoology URL and output folder in the application.

WINDOWS - LATER RUNS
--------------------
Double-click run.bat.

MACOS - FIRST RUN
-----------------
1. Install Python 3.10 or newer if `python3 --version` does not work in Terminal.
2. Double-click install_and_run.command.
3. If macOS blocks it, right-click the file and choose Open. Alternatively run:

     ./install_and_run.command

The normal GUI opens when the selected Python includes Tkinter. If Tkinter is
not installed (as with some Homebrew Python installations), the downloader
automatically uses an equivalent Terminal interface.

MACOS - LATER RUNS
------------------
Double-click run.command, or run:

  ./run.command

The .sh launchers are also available for users who prefer a shell.

PLATFORM-LOCAL ENVIRONMENTS
---------------------------
The installers keep dependencies inside the project:

  Windows: .venv-windows
  macOS:   .venv-macos

This prevents package conflicts and lets the same project folder be used on
both operating systems.

LOGIN AND SAVED CREDENTIALS
---------------------------
The same Playwright browser profile is reused between runs, so an unexpired
Schoology/Microsoft session normally remains signed in.

You can optionally provide your Microsoft/Schoology email and password. Select
the remember option when using the GUI, or answer yes in the Terminal interface.
When Terminal asks for the password, typing is intentionally invisible: no dots,
stars, or letters appear. Type the password normally and press Return.
The password is stored through the operating system credential vault:

  Windows: Windows Credential Manager
  macOS:   Keychain (through Apple's built-in security tool)

It is NOT written to settings.json, the download state, or log files. The app
only fills credentials on the selected Schoology host or Microsoft's official
login hosts. MFA still requires your approval when Microsoft requests it.

When you approve password storage, it is written to the credential vault before
the browser opens. Automatic sign-in waits through redirects and handles native
Schoology forms, Microsoft account selection, password entry, and the Microsoft
"Stay signed in" confirmation. Captcha, MFA, passkey, and organization approval
screens remain interactive for security.

DOWNLOAD FIXES
--------------
- Removes Windows-invalid control characters/newlines from path components.
- Never turns the Schoology navigation menu into a directory tree.
- Rejects links belonging to a different Schoology course.
- Saves each PDF from a single immediate request so signed CDN URLs do not expire
  between a separate confirmation request and the actual download.
- Preserves original PDF filenames, spaces, and capitalization.
- Adds [2], [3], and so on only for genuinely different same-name files.
- Skips completed identical files on later runs.
- Finds files in page links and authenticated browser network responses.
- Writes a timestamped diagnostic log without passwords or signed URL tokens.

APPLICATION DATA
----------------
The reusable browser profile and non-secret settings are stored under:

  Windows: %LOCALAPPDATA%\SchoologyPDFDownloader
  macOS:   ~/.schoology_pdf_downloader

The browser profile contains login cookies, like a normal browser profile. Keep
your operating-system account protected. Deleting this directory signs the app
out locally.

NOTES
-----
- Close other copies of the downloader before starting; only one process can use
  its browser profile at a time.
- The first installer run downloads Python packages and Playwright Chromium.
- A live run requires your own authorized Schoology account and course access.
