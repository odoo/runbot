ADD: stagings reverse index (from commits)

Finding out the commits from a staging is not great but it's easy enough, the
reverse was difficult and very inefficient. Splat out the "heads" JSON field
into two join tables, and provide both ORM methods and a JSON endpoint to
lookup stagings based on their commits.
