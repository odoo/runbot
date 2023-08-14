IMP: optimize home page

An unnecessary deopt and a few opportunities were found and fixed in the home
page / main dashboard, a few improvements have been implemented which should
significantly lower the number of SQL queries and the time needed to generate
the page.
