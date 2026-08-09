---
name: A click hit the wrong element
about: The highest-priority bug in this project. Please report it.
title: 'Wrong element: '
labels: wrong-element
---

<!-- This is the failure mode ZeroDOM exists to prevent, so it jumps the queue.
     Thank you for taking the time. -->

**URL** (or attach a minimal HTML file that reproduces it)

**What should have been clicked**

**What was clicked instead**

**The node, from the graph**
```
[NN] type 'label'
```

**Selector it resolved to** — from `zerodom <url> --json`, or the `--html`
report, which shows every node's target visually:

```bash
zerodom <url> --html report.html
```

**Environment**
- zerodom version:
- Python version:
- Sync or async Playwright page:
- Was `frames=True` set?
