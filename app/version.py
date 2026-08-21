"""The build number, in one place.

Every build bumps this by 0.1, in BOTH repositories, and the admin dashboard
shows the two side by side. The point is not the number: it is being able to
tell, in five seconds and without trusting anyone's word, whether the code you
are looking at a bug in is the code that was supposed to fix it.

That question has been unanswerable so far. A backend can sit twelve days behind
its frontend and nothing on the screen says so — every symptom then looks like a
new bug rather than an old one that was already fixed but never deployed.

Bump BOTH files together:
    olliev6/app/version.py          VERSION
    ollie-v5-frontend/lib/version.ts APP_VERSION
"""

VERSION = "8.93"

# The day this build was cut. Shown next to the number, because "v1.1" answers
# which build and this answers how old it is.
BUILT_AT = "2026-08-21"
