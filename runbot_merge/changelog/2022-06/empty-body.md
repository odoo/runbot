FIX: correctly handle PR empty PR descriptions

Github's webhook for this case are weird, and weren't handled correctly,
updating a PR's description to *or from* empty might be mishandled.
