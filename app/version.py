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

THE NUMBERING RESTARTS AFTER THIS BUILD. The 9.x line ran out of room — it
ended at 9.9999998, eight digits deep, and a number nobody can read at a glance
cannot do the job described above. 9.99 is the last of it.

    9.99    this build — the end of the old line
    1.0     the next one
    1.1     then
    1.12    then

Going from 9.99 to 1.0 is not a downgrade. The 9.x numbers were never releases,
they were rebuilds during the build-out; 1.0 is the first version of the product
proper. Do not "fix" the sequence back to 10.0.
"""

VERSION = "1.30"

# The day this build was cut. Shown next to the number, because "v1.1" answers
# which build and this answers how old it is.
BUILT_AT = "2026-08-29"
