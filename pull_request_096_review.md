PR #96 - Review Notes and Testing Instructions

Title:
feat: cookie/yt-dlp compat + diagnose tools

Summary of changes:
- Added modules/tools/convert_cookies.py: heuristic converter to turn various exported cookie formats into Netscape cookies.txt for yt-dlp compatibility.
- Added tools/diagnose_env.py: quick environment check for yt-dlp/ffmpeg and cookie presence; runs a gentle yt-dlp format-list test and outputs JSON.
- Modified modules/youtube_handler.py: added helpers to detect if a cookie file is Netscape format and, if not, auto-convert to a temporary Netscape file for use with yt-dlp (safe default: does not overwrite original cookie files).

Why this helps:
Some users export cookies in non-Netscape formats (CSV/tabular/extension formats) that yt-dlp does not understand. This change auto-converts such files at runtime to avoid download failures due to unrecognized cookie formats.

What I (the contributor) have done for you (so you don't need to):
1) Prepared the branch and committed changes to chenglujiang46-cyber:fix/cookie-yt-dlp-compat.
2) Created this review-notes file in the branch so maintainers and reviewers can see testing instructions directly in the PR's changeset.
3) I will monitor any logs you paste here and push fixes to the same branch as needed — you do not need to run or modify code unless you want to.

How to test locally (recommended steps):
1. Run environment diagnostic (from project root):
   python tools/diagnose_env.py
   - Please copy the entire JSON output and paste it into the PR comments or here; that will let me diagnose environment/tool issues quickly.

2. (Optional) Manually convert your current YouTube cookie file and validate:
   python modules/tools/convert_cookies.py cookies/yt_cookies.txt cookies/yt_cookies_netscape.txt
   yt-dlp --cookies cookies/yt_cookies_netscape.txt -F "https://www.youtube.com/watch?v=VIDEO_ID"
   - If the converted file lists formats successfully, the converter worked.

3. Trigger a real download task via the app/UI (or call download_video_data), then collect the task log:
   - The app writes logs to logs/task_<task_id>.log. Please copy the section that contains yt-dlp stdout/stderr (especially lines with "ERROR:", "HTTP Error 403", "Signature extraction failed", or "The page needs to be reloaded.") and paste here.

What I'll do after you paste diagnostic output or logs:
- If diagnose_env.py shows missing yt-dlp/ffmpeg or obvious errors, I'll push fixes or more helpful error messages to the branch.
- If logs show yt-dlp runtime errors, I'll patch the branch to add more robust fallbacks (e.g., tweak arguments, improve cookie parsing) and push commits to this branch; those commits will automatically appear in PR #96.

If you prefer me to merge the PR once tests pass:
- Tell me "merge when green" and I'll request final checks and attempt to merge when it's ready (note: only repository maintainers or users with merge rights can complete the merge; if you want me to merge and you have permissions, I can do it for you).

Notes & safety:
- The converter writes temporary files next to the original cookie file by default; it does not overwrite original cookie exports.
- All changes are limited to the branch fix/cookie-yt-dlp-compat in your fork and will only be merged into the original repo when maintainers accept the PR.

I'll wait for either:
- The JSON output from python tools/diagnose_env.py, or
- The logs from a failed download (logs/task_<task_id>.log), or
- Your instruction to "merge when green".

Rest and don't worry — I will continue from here once you paste the diagnostic output or logs, or tell me to proceed to merge.