# VHS tape — scripted terminal GIF of ZeroDOM catching a broken-access-control
# bug on a PortSwigger lab. Deterministic and repeatable: it just runs the same
# read-only `zerodom compare` as run.sh and records it.
#
#   1. Produce proof/user.json + proof/admin.json (see run.sh header).
#   2. export LAB=https://<your-instance>.web-security-academy.net
#   3. vhs proof/demo.tape   ->   proof/demo.gif
#
# Install vhs once: https://github.com/charmbracelet/vhs (`go install
# github.com/charmbracelet/vhs@latest`, or a release binary).

Output proof/demo.gif
Set Shell bash
Set FontSize 17
Set Width 1200
Set Height 760
Set Padding 20
Set Framerate 20
Set Theme "Catppuccin Mocha"

# Hidden setup: use the repo's venv if present so `zerodom` is on PATH.
# Harmless elsewhere (falls through to a pip-installed zerodom).
Hide
Type "source .venv/bin/activate 2>/dev/null; clear" Enter
Show

Type "# ZeroDOM: is the admin panel access-controlled, or just hidden?"
Enter
Sleep 1500ms

Type 'zerodom compare "$LAB/admin" --as user=proof/user.json --as admin=proof/admin.json | python3 -m json.tool'
Enter
Sleep 7s

Type "# 403 for user, 200 for admin — and only_admin lists the delete-user controls."
Enter
Sleep 4s
