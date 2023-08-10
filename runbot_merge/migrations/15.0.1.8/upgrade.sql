CREATE TABLE runbot_merge_stagings_commits (
    id serial NOT NULL,
    staging_id integer not null references runbot_merge_stagings (id),
    commit_id integer not null references runbot_merge_commit (id),
    repository_id integer not null references runbot_merge_repository (id)
);

CREATE TABLE runbot_merge_stagings_heads (
    id serial NOT NULL,
    staging_id integer NOT NULL REFERENCES runbot_merge_stagings (id),
    commit_id integer NOT NULL REFERENCES runbot_merge_commit (id),
    repository_id integer NOT NULL REFERENCES runbot_merge_repository (id)
);

-- some of the older stagings only have the head, not the commit,
-- add the commit
UPDATE runbot_merge_stagings
  SET heads = heads::jsonb || jsonb_build_object(
    'odoo/odoo^', heads::json->'odoo/odoo',
    'odoo/enterprise^', heads::json->'odoo/enterprise'
  )
  WHERE heads NOT ILIKE '%^%';

-- some of the stagings have heads which don't exist in the commits table,
-- because they never got a status from the runbot...
-- create fake commits so we don't lose heads
INSERT INTO runbot_merge_commit (sha, statuses, create_uid, create_date, write_uid, write_date)
    SELECT r.value, '{}', s.create_uid, s.create_date, s.create_uid, s.create_date
    FROM runbot_merge_stagings s,
         json_each_text(s.heads::json) r
ON CONFLICT DO NOTHING;

CREATE TEMPORARY TABLE staging_commits (
    id integer NOT NULL,
    repo integer NOT NULL,
    -- the staging head (may be a dedup, may be the same as commit)
    head integer NOT NULL,
    -- the staged commit
    commit integer NOT NULL
);
-- the splatting works entirely off of the staged head
-- (the one without the ^ suffix), we concat the `^` to get the corresponding
-- merge head (the actual commit to push to the branch)
INSERT INTO staging_commits (id, repo, head, commit)
    SELECT s.id, re.id AS repo, h.id AS head, c.id AS commit
    FROM runbot_merge_stagings s,
         json_each_text(s.heads::json) r,
         runbot_merge_commit h,
         runbot_merge_commit c,
         runbot_merge_repository re
    WHERE r.key NOT ILIKE '%^'
      AND re.name = r.key
      AND h.sha = r.value
      AND c.sha = s.heads::json->>(r.key || '^');

INSERT INTO runbot_merge_stagings_heads (staging_id, repository_id, commit_id)
SELECT id, repo, head FROM staging_commits;

INSERT INTO runbot_merge_stagings_commits (staging_id, repository_id, commit_id)
SELECT id, repo, commit FROM staging_commits;

ALTER TABLE runbot_merge_stagings DROP COLUMN heads;
