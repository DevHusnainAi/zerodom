# VHS tape — scripted terminal GIF of `zerodom sweep` catching broken access
# control with two identities, on a local fixture app (proof/sweep_demo/).
# No PortSwigger login, no screen recording: it just runs the real, read-only
# sweep and records the terminal. Deterministic and repeatable.
#
#   vhs proof/sweep.tape   ->   proof/sweep.gif
#
# Install vhs once: https://github.com/charmbracelet/vhs

Output proof/sweep.gif
Set Shell bash
Set FontSize 15
Set Width 1840
Set Height 210
Set Padding 22
Set Framerate 20
Set Theme "Catppuccin Mocha"

# Hidden setup: venv on PATH + start the vulnerable fixture in the background.
Hide
Type "source .venv/bin/activate 2>/dev/null; python3 proof/sweep_demo/app.py >/dev/null 2>&1 & sleep 1; clear" Enter
Show

Type "# Two logins. Does /admin enforce access control, or just hide the link?"
Enter
Sleep 1500ms

Type "zerodom sweep http://127.0.0.1:8799 --as user=proof/sweep_demo/user.json --as admin=proof/sweep_demo/admin.json --format md"
Enter
Sleep 8s

Type "# admin got 200 where user got 401 on /admin — access controlled by a"
Enter
Type "# client-controllable cookie. Deterministic replay proves it. You confirm."
Enter
Sleep 4s

Hide
Type "kill %1 2>/dev/null" Enter
Show
