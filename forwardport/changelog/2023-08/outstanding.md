IMP: outstandings page

- increased time-before-outstanding from 3 to 7 days, as 3~4 days is common in
  normal operations, especially when merging from very low branches were
  forward-porting may take a while
- improved performances by optimising fetching & filtering
- added counts to the main listing for clarity (instead of hiding them in a
  popover)
- added the *original authors* for the outstanding forward ports
- added ability to filter by team, if such are configured
