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
            
            if thread_texts and len(thread_texts) > 1:
                print("\n[THREAD AUTOMATION] Fetching the parent tweet URL automatically...")
                tweet_id = self._get_tweet_url_automatically()
                
                if not tweet_id:
                    print("[THREAD AUTOMATION] Aborted due to failure to get tweet ID.")
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
            print("\n(Note: To enable 100% hands-free posting, run: pip install pyautogui pyperclip)")
        except Exception as e:
            print(f"\n[ERROR] GUI Automation failed: {e}")
            print("You may need to manually click 'Post' in the opened browser tab.")
        
        return f"intent-post-{int(time.time())}"

    def _get_tweet_url_automatically(self) -> str | None:
        try:
            import pyautogui
            import pyperclip
            import re

            handle = self.settings.twitter_handle
            if not handle:
                print("[ERROR] TWITTER_HANDLE not set in config. Cannot automatically fetch tweet URL.")
                return None

            # 1. Wait 7 seconds for the tweet to finish posting.
            time.sleep(7)

            # 2. Open a new tab to https://x.com/{TWITTER_HANDLE}/with_replies
            replies_url = f"https://x.com/{handle}/with_replies"
            webbrowser.open(replies_url)

            # 3. Wait 8 seconds for the page to load.
            time.sleep(8)

            # 4. Click the center of the screen to ensure focus.
            screen_width, screen_height = pyautogui.size()
            pyautogui.click(screen_width // 2, screen_height // 2)

            # 5. Press `j` (X.com's built-in keyboard shortcut to select the top tweet).
            pyautogui.press('j')
            time.sleep(1) # tiny delay just in case

            # 6. Press `Enter` (X.com shortcut to open the selected tweet).
            pyautogui.press('enter')

            # 7. Wait 4 seconds for the tweet page to load.
            time.sleep(4)

            # 8. Press `Ctrl+L` (Focuses your browser's address bar).
            pyautogui.hotkey('ctrl', 'l')
            time.sleep(0.5)

            # 9. Press `Ctrl+C` (Copies the URL to your clipboard).
            pyautogui.hotkey('ctrl', 'c')
            time.sleep(0.5)

            # 10. Python reads the clipboard using the pyperclip library to extract the Tweet ID!
            clipboard_content = pyperclip.paste()
            print(f"[DEBUG] Clipboard content: {clipboard_content}")

            match = re.search(r'(?:x\.com|twitter\.com)/[^/]+/status/(\d+)', clipboard_content)
            if match:
                tweet_id = match.group(1)
                print(f"[SUCCESS] Extracted Tweet ID: {tweet_id}")
                return tweet_id
            
            print("[ERROR] Could not extract Tweet ID from clipboard content.")
            return None
        except Exception as e:
            print(f"[ERROR] Failed to automatically fetch tweet URL: {e}")
            return None
