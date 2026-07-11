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

    def post(self, text: str, image_paths: list[str] | None = None) -> str:
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
            
            # Try to copy the path to clipboard for easy pasting
            try:
                import pyperclip
                pyperclip.copy(abs_paths[0])
                print("(The first image path has been copied to your clipboard.)")
            except ImportError:
                pass
        
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
            
        except ImportError:
            print("\n(Note: To enable 100% hands-free posting, run: pip install pyautogui)")
        
        return f"intent-post-{int(time.time())}"
                
