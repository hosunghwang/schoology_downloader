Schoology File Downloader v6.0
==============================

My kid's school uses Schoology for study materials and assignments. Manually downloading and
organizing the materials from the website is tedious.
This app automates the process by downloading the materials to a folder on my computer, allowing
me to keep a local copy of PDF, PowerPoint (.pptx), and Word (.docx) files for personal use.


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

By default, the downloader scans only the exact Schoology URL you provide. To
scan all linked folders and material pages in the same course, either select
the "Scan all linked folders/material pages" checkbox in the GUI or run:

  ./run.command --all

Terminal-only examples:

  ./run.command --cli
  ./run.command --cli --all

On Windows, the equivalent Command Prompt command is:

  run.bat --all

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
- Scans only the exact provided URL by default; --all enables the recursive
  whole-course scan.
- Removes Windows-invalid control characters/newlines from path components.
- Never turns the Schoology navigation menu into a directory tree.
- Rejects links belonging to a different Schoology course.
- Saves each supported file from a single immediate request so signed CDN URLs do not expire
  between a separate confirmation request and the actual download.
- Downloads and validates .pdf, .pptx, and .docx files.
- Extracts original Word/PowerPoint download URLs embedded in Schoology's
  document viewer instead of saving only its generated PDF preview.
- Uses Schoology breadcrumbs and the material's parent-folder link so a direct
  material URL is saved under its actual folder hierarchy.
- In --all mode, opens directly linked material-detail pages from a linked
  source course as leaf pages without crawling into that other course.
- Preserves original filenames, spaces, capitalization, and file extensions.
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
