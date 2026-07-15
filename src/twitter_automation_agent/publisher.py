import time
import urllib.parse
import webbrowser
from pathlib import Path

from twitter_automation_agent.config import Settings


class XPublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def verify_credentials(self) -> tuple[str | None, str | None]:
        """
        No credentials needed for the intent URL method.
        """
        return "system_browser", "ready"

    def post(self, text: str, image_paths: list[str] | None = None, thread_texts: list[str] | None = None, telegram_sender=None) -> str:
        """
        Automates the Twitter web interface using the foolproof Intent URL method.
        This opens a new tab in the user's ACTUAL default browser where they are already logged in.
        """
        # We URL-encode the tweet text so it can be passed in the URL
        encoded_text = urllib.parse.quote(text)
        
        intent_url = f"https://x.com/intent/tweet?text={encoded_text}"
        
        print("\n\n[SUCCESS] Opening X.com in your default system browser!")
        print("Your tweet has been automatically pasted into the box.")
        
        if image_paths:
            abs_paths = [str(Path(p).absolute()) for p in image_paths]
            print("\n[IMAGE ATTACHMENT REQUIRED]")
            print("Twitter Intent URLs cannot attach images automatically.")
            print(f"Please drag and drop these images into your Tweet:\n" + "\n".join(abs_paths))
            
        if thread_texts and len(thread_texts) > 1:
            print("\n[THREAD DETECTED]")
            print("Twitter Intent URLs cannot natively post a 5-tweet thread.")
            print("We will automate pasting the FIRST tweet and attaching the images.")
            print("Then we will ask you for the URL of the posted tweet on Telegram to continue the thread.")
                
        print("\nClick the 'Post' button on X.com to finish!")
        
        # Open the URL in the default browser
        webbrowser.open(intent_url)
        
        try:
            import pyautogui
            import subprocess
            
            print("\n[AUTOMATION] Please do not touch your mouse/keyboard for 10 seconds...")
            # Wait 10 seconds for the Chrome tab to fully open and load the X.com compose box
            time.sleep(10)
            
            if image_paths:
                # Copy the actual image data (pixels) into the Windows clipboard
                # This ensures it pastes correctly as an image attachment in X.com
                ps_command = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$sc = New-Object System.Collections.Specialized.StringCollection; "
                )
                for p in abs_paths:
                    ps_command += f"$sc.Add('{p}'); "
                ps_command += "[System.Windows.Forms.Clipboard]::SetFileDropList($sc)"
                
                subprocess.run(["powershell", "-Sta", "-command", ps_command], check=False)
                
                # Paste the image directly into the active X.com tweet box
                pyautogui.hotkey('ctrl', 'v')
                
                # Wait 8 seconds for the image thumbnail to fully upload, attach, and render
                # (If you hit post too early, X.com complains that the media is still attaching)
                time.sleep(8)
                
            # Press Ctrl+Enter (the X.com hotkey to immediately publish the tweet)
            pyautogui.hotkey('ctrl', 'enter')
            
            print("Tweet automatically posted!")
            
            if thread_texts and len(thread_texts) > 1 and telegram_sender:
                print("\n[THREAD AUTOMATION] Waiting for the parent tweet URL from Telegram...")
                tweet_id = self._wait_for_tweet_url_from_telegram(telegram_sender)
                
                if not tweet_id:
                    print("[THREAD AUTOMATION] Aborted due to timeout or error.")
                else:
                    for i, next_tweet in enumerate(thread_texts[1:]):
                        if i > 0:
                            print(f"[THREAD AUTOMATION] Waiting 3 minutes before posting the next reply...")
                            time.sleep(180) # 3 minute interval
                            
                        encoded_next = urllib.parse.quote(next_tweet)
                        reply_url = f"https://x.com/intent/tweet?in_reply_to={tweet_id}&text={encoded_next}"
                        
                        print(f"[THREAD AUTOMATION] Opening reply intent for next tweet...")
                        webbrowser.open(reply_url)
                        time.sleep(10) # wait for compose box
                        
                        pyautogui.hotkey('ctrl', 'enter')
                        print(f"[THREAD AUTOMATION] Reply posted!")
                    
        except ImportError:
            print("\n(Note: To enable 100% hands-free posting, run: pip install pyautogui)")
        
        return f"intent-post-{int(time.time())}"

    def _wait_for_tweet_url_from_telegram(self, telegram_sender) -> str | None:
        if not telegram_sender:
            return None
            
        telegram_sender.send_text(
            "Please reply to this message with the URL of the tweet you just posted on X.com so I can post the next one in the thread! (Timeout: 5 minutes)"
        )
        
        start_time = time.time()
        timeout = 300 # 5 minutes
        offset = None
        
        # Drain pending updates first
        try:
            updates = telegram_sender.get_updates(offset=offset, timeout=1)
            if updates:
                offset = updates[-1].get("update_id", 0) + 1
        except Exception:
            pass
            
        while time.time() - start_time < timeout:
            try:
                updates = telegram_sender.get_updates(offset=offset, timeout=10)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    
                    message = update.get("message")
                    if not message:
                        continue
                        
                    text = str(message.get("text") or "").strip()
                    
                    import re
                    match = re.search(r'(?:x\.com|twitter\.com)/[^/]+/status/(\d+)', text)
                        
                    if match:
                        telegram_sender.send_text("Got it! Posting the next tweet now...")
                        return match.group(1)
            except Exception as e:
                print(f"[DEBUG] Polling error: {e}")
                time.sleep(5)
                
        telegram_sender.send_text("Timed out waiting for tweet URL. Aborting the rest of the thread.")
        return None
                
