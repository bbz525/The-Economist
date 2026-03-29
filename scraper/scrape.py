import os
import re
import sys
import shutil
import hashlib
import subprocess
import requests
import py7zr
import resend
import urllib.parse
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Configuration
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
URL = "https://www.920.im/the-economist-ebook-audio-weekly-update/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
REPO_DIR = os.environ.get("ECONOMIST_REPO_DIR", os.path.join(SCRIPT_DIR, "economist_repo"))
resend.api_key = os.environ.get("RESEND_API_KEY")

def send_notification(subject, html_content):
    if not resend.api_key:
        print("RESEND_API_KEY not set. Skipping email notification.")
        return

    from_email = os.environ.get("RESEND_FROM") or "onboarding@resend.dev"
    to_email = os.environ.get("RESEND_TO")
    
    if not to_email:
        print("RESEND_TO not set. Cannot send notification.")
        return

    try:
        r = resend.Emails.send({
            "from": from_email,
            "to": to_email,
            "subject": subject,
            "html": html_content
        })
        print(f"Notification sent successfully: {r}")
    except Exception as e:
        print(f"Failed to send email notification: {e}")

def get_latest_link_info():
    print(f"Fetching {URL}...")
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(URL, headers=headers)
    response.raise_for_status()

    html = response.text

    date_pattern = re.compile(r"The Economist (\d{4}-\d{2}-\d{2})", re.IGNORECASE)
    soup = BeautifulSoup(html, 'html.parser')
    text_content = soup.get_text()

    dates = date_pattern.findall(text_content)
    if not dates:
        print("Could not find any dates matching 'The Economist yyyy-mm-dd'.")
        return None

    latest_date = sorted(dates, reverse=True)[0]
    print(f"Found latest date: {latest_date}")

    md5_pattern = re.compile(f"The Economist.*?{latest_date}.*?Ebook.*?MD5[：:]\\s*([a-fA-F0-9]{{32}})", re.IGNORECASE | re.DOTALL)
    match = md5_pattern.search(text_content)
    ebook_md5 = match.group(1).lower() if match else None
    print(f"MD5 for Ebook: {ebook_md5}")

    # Search for Buzz and Rano links that contain "Ebook" in their text
    buzz_link = None
    rano_link = None

    for a in soup.find_all('a'):
        a_text = a.get_text()
        href = a.get('href', '')

        if 'Ebook' in a_text:
            real_url = href
            if 'url=' in href:
                real_url = urllib.parse.unquote(href.split('url=')[-1])

            if 'buzzheavier.com' in real_url and not buzz_link:
                buzz_link = real_url
            elif 'ranoz.gg' in real_url and not rano_link:
                rano_link = real_url

    print(f"Buzz Ebook link found: {buzz_link}")
    print(f"Rano Ebook link found: {rano_link}")

    return {
        "date": latest_date,
        "md5": ebook_md5,
        "buzz": buzz_link,
        "rano": rano_link
    }

def verify_md5(filepath, expected_md5):
    if not expected_md5:
        print("No expected MD5 provided, skipping check.")
        return True

    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)

    file_md5 = hash_md5.hexdigest()
    print(f"Calculated MD5: {file_md5} | Expected: {expected_md5}")
    return file_md5 == expected_md5

def download_file_with_playwright(url, destination_dir, max_click_attempts=30):
    print(f"Starting Playwright to download from: {url}")
    os.makedirs(destination_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)

        # Block popups
        def handle_page(new_page):
            # Only close if it's not the initial page we just opened
            print(f"New page intercepted: {new_page.url}")
            # we can wait for a bit to see if we can identify it, 
            # but usually we just close any page that isn't the first one
            # Unfortunately, `context.on("page")` also fires for our `context.new_page()`.
            pass # We will handle popups using new mechanism if needed. Let Playwright block popups via context options instead if possible, or just close it if len(pages) > 1

        # A safer way to block popups is to listen but check pages length:
        def on_page_open(new_page):
            if len(context.pages) > 1:
                print(f"Popup intercepted and will be closed: {new_page.url}")
                new_page.close()

        context.on("page", on_page_open)

        page = context.new_page()
        print("Navigating to URL...")

        # Sometimes it's a 920.im go.html redirect or CloudFlare challenge
        page.goto(url, wait_until="domcontentloaded", timeout=90000)

        try:
            # Wait up to 30s for the page state to stabilize (e.g., after CF JS challenge)
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass # ignore

        print("Waiting extra time to clear potential Cloudflare bot checks...")
        page.wait_for_timeout(10000)

        print("Looking for download button...")

        # Depending on BuzzHeavier or Ranoz, the download button varies
        download_info = None
        page.wait_for_timeout(5000) # Give the page some time to stabilize
        # Use an event listener so we don't block on expect_download, making retries much faster
        downloads = []
        def on_download(dl):
            downloads.append(dl)

        page.on("download", on_download)

        for attempt in range(max_click_attempts):
            if downloads:
                break

            print(f"Attempt {attempt + 1} to trigger download...")
            try:
                # Buzzheavier / Ranoz typically have a button "Download" or "Free Download"
                loc = page.locator("[hx-get*='/download'], button:has-text('Download'), a:has-text('Download'), .download-btn, #download-btn").first
                if loc.is_visible():
                    print("Found a 'Download' button, clicking...")
                    loc.click(force=True)
                else:
                    print("No explicit 'Download' button found immediately.")
            except Exception as e:
                print(f"Failed to click on this attempt: {e}")

            # Wait a short time to see if download starts or popup opens
            page.wait_for_timeout(2000)

        if not downloads:
            print("Failed to trigger download after multiple attempts.")
            return None

        download = downloads[0]
        filename = download.suggested_filename
        print(f"Download started... filename: {filename}")

        # Ensure it's downlaoding fully without interruption
        # Since it says Wait until no popups and the file downloaded correctly
        # Playwright isolates downloads nicely.

        filepath = os.path.join(destination_dir, filename)
        download.save_as(filepath)
        print(f"Download complete: {filepath}")

        browser.close()
        return filepath

def extract_and_rename_and_push(downloaded_file, date_str):
    # Setup working dir
    work_dir = os.path.dirname(downloaded_file)
    original_filename = os.path.basename(downloaded_file)

    # Rename logic as requested by user -> rename dir and file to TE-yyyy-mm-dd
    new_dir_name = f"TE-{date_str}"

    # We extract to a temporary directory first
    extract_dir = os.path.join(work_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)

    print(f"Extracting {downloaded_file} using py7zr...")
    with py7zr.SevenZipFile(downloaded_file, mode='r') as archive:
        archive.extractall(path=extract_dir)

    # We rename all extracted files AND package them into REPO_DIR / TE-yyyy-mm-dd / TE-yyyy-mm-dd.ext

    target_repo_folder = os.path.join(REPO_DIR, new_dir_name)
    os.makedirs(target_repo_folder, exist_ok=True)
    print(f"Moving and renaming to repository folder: {target_repo_folder}")

    # Walk through the extracted files and move them
    for root, dirs, files in os.walk(extract_dir):
        for file in files:
            file_path = os.path.join(root, file)
            # e.g., if extracted "The Economist 2026-02-14.pdf", we rename to "TE-2026-02-14.pdf"
            ext = os.path.splitext(file)[1]
            new_file_name = f"TE-{date_str}{ext}"
            new_file_path = os.path.join(target_repo_folder, new_file_name)

            # If multiple files of the same extension exist in archive, we might overwrite, 
            # so we could append a counter, but usually there's 1 PDF and 1 EPUB.
            if os.path.exists(new_file_path):
                base, e = os.path.splitext(new_file_name)
                # Just add some uniqueness if collision happens
                import uuid
                new_file_name = f"{base}-{str(uuid.uuid4())[:4]}{e}"
                new_file_path = os.path.join(target_repo_folder, new_file_name)

            os.rename(file_path, new_file_path)
            print(f"Renamed {file} -> {new_file_name}")

    cleanup_old_issues(max_issues=12)

    print("Pushing to GitHub with Token...")
    github_token = os.environ.get("GITHUB_TOKEN")
    github_username = os.environ.get("GITHUB_USERNAME")
    github_repo = os.environ.get("GITHUB_REPO")

    cwd = REPO_DIR

    subprocess.run(["git", "add", "."], cwd=cwd, check=True)
    subprocess.run(["git", "commit", "-m", f"Add {new_dir_name} and cleanup old issues"], cwd=cwd, check=False) # OK if nothing to commit

    if github_token and github_username and github_repo:
        print(f"Configuring git remote with token for {github_repo}...")
        remote_url = f"https://{github_username}:{github_token}@github.com/{github_username}/{github_repo}.git"

        # We first try to add the remote, if it fails, we set the url (meaning it already exists)
        try:
            subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=cwd, check=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=cwd, check=True)

        push_command = ["git", "push", "-u", "origin", "HEAD"]
    else:
        print("Note: GITHUB configuration is incomplete in .env. Attempting regular git push...")
        push_command = ["git", "push"]

    subprocess.run(push_command, cwd=cwd, check=True)
    print("Push successful.")

def cleanup_old_issues(max_issues=12):
    if not os.path.exists(REPO_DIR):
        return

    print(f"Checking for old issues in {REPO_DIR} to enforce {max_issues} max retention policy...")
    # Find all directories that match the TE-YYYY-MM-DD pattern
    issue_dirs = []
    pattern = re.compile(r"^TE-\d{4}-\d{2}-\d{2}$")

    for item in os.listdir(REPO_DIR):
        full_path = os.path.join(REPO_DIR, item)
        if os.path.isdir(full_path) and pattern.match(item):
            issue_dirs.append(full_path)

    # Sort them by name, which effectively sorts by date since it's YYYY-MM-DD
    issue_dirs.sort(reverse=True)

    # If we have more than max_issues, delete the older ones
    if len(issue_dirs) > max_issues:
        dirs_to_delete = issue_dirs[max_issues:]
        for old_dir in dirs_to_delete:
            print(f"Removing old issue directory: {old_dir}")
            try:
                # Use git rm if it's a git repo to keep tree clean
                if os.path.exists(os.path.join(REPO_DIR, ".git")):
                    subprocess.run(["git", "rm", "-r", old_dir], cwd=REPO_DIR, check=True, stdout=subprocess.DEVNULL)
                else:
                    shutil.rmtree(old_dir)
            except Exception as e:
                print(f"Failed to remove {old_dir}: {e}")

def main():
    download_dir = os.path.join(SCRIPT_DIR, "downloads")
    try:
        info = get_latest_link_info()
        if not info:
            print("Failed to get info.")
            send_notification("Cronjob Failure: Economist Downloader", "<p>Failed to get info from the source.</p>")
            sys.exit(1)

        target_dir = os.path.join(REPO_DIR, f"TE-{info['date']}")
        if os.path.exists(target_dir):
            print(f"Directory {target_dir} already exists. Skipping download.")
            send_notification(
                f"Cronjob Skip: Economist Downloader {info['date']}", 
                f"<p>The issue for <strong>The Economist {info['date']}</strong> already exists in your repository.</p><p>No new updates needed.</p>"
            )
            return # Exit successfully without error

        downloaded_file = None

        # User requested specific fallback Sequence: Buzz (4) -> Rano (5) -> Buzz (4)
        sequence = []
        if info['buzz']:
            sequence.append(("Buzz", info['buzz'], 4))
        if info['rano']:
            sequence.append(("Rano", info['rano'], 5))
        if info['buzz']:
            sequence.append(("Buzz (Retry)", info['buzz'], 4))

        if not sequence:
            print("No download link found.")
            send_notification("Cronjob Failure: Economist Downloader", "<p>No download link found for the latest issue (neither Buzz nor Ranoz).</p>")
            sys.exit(1)

        for name, url, attempts in sequence:
            print(f"--- Attempting download from {name} (Max Clicks: {attempts}) ---")
            try:
                downloaded_file = download_file_with_playwright(url, download_dir, max_click_attempts=attempts)
                if downloaded_file:
                    print(f"Successfully downloaded from {name}")
                    break
            except Exception as e:
                print(f"Error downloading from {name} ({url}): {e}")

        if not downloaded_file:
            print("Failed to download from all available sources in the sequence.")
            send_notification(
                "Cronjob Failure: Economist Downloader", 
                "<p>Failed to download from all available sources.</p><p>Sequence tried: Buzz(4) -> Rano(5) -> Buzz(4).</p>"
            )
            sys.exit(1)

        if info['md5']:
            is_valid = verify_md5(downloaded_file, info['md5'])
            if not is_valid:
                print("MD5 mismatch! Exiting.")
                send_notification("Cronjob Failure: Economist Downloader", f"<p>MD5 checksum mismatch for {downloaded_file}.</p>")
                sys.exit(1)

        extract_and_rename_and_push(downloaded_file, info['date'])

        # Success notification
        send_notification(
            f"Cronjob Success: Economist Downloader {info['date']}", 
            f"<p>Successfully downloaded, extracted, and pushed <strong>The Economist {info['date']}</strong>.</p><p>MD5 Verified: {info['md5']}</p>"
        )


    except Exception as e:
        print(f"Unexpected error: {e}")
        send_notification("Cronjob Error: Economist Downloader", f"<p>An unexpected error occurred during execution:</p><pre>{e}</pre>")
        sys.exit(1)
    finally:
        if os.path.exists(download_dir):
            shutil.rmtree(download_dir, ignore_errors=True)
            print("Cleaned up temporary downloads directory.")

if __name__ == "__main__":
    main()
