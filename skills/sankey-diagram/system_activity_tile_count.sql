-- Looker System Activity: dashboard tile counts
--
-- PURPOSE
-- Fills the gaps that LookML alone cannot answer when planning a Looker to Omni
-- migration. The repo only contains code-defined dashboards, so user-generated
-- content (UGC) created in the Looker UI is invisible to /looker-erd and
-- /sankey-diagram. This query reaches into Looker's own System Activity database
-- to list dashboards with their tile counts, owner, and legacy-viewer flag.
--
-- WHERE TO RUN
-- Looker System Activity (the internal Looker database, MySQL dialect).
-- Admin access required. Paste as SQL Runner against the System Activity
-- connection, or rebuild it in the System Activity explores.
--
-- WHAT YOU USE IT FOR
-- 1. Sizing: multiply total tiles by roughly 10 minutes to estimate migration
--    effort across the whole estate. Do not estimate per dashboard; some tiles
--    are copy-paste and some are an hour of merge-query work. It averages out.
-- 2. UGC discovery: find dashboards that exist in Looker but not in the repo.
-- 3. Complexity scoring: tile count and merge-query count feed the H/M/L/XS
--    ratings used by /sankey-diagram via dashboard-complexity.csv.
--
-- IMPORTANT CAVEATS, READ BEFORE TRUSTING THE OUTPUT
--
-- * This is NOT a complete dashboard inventory. The HAVING clause keeps only
--   dashboards that trip at least one Looker performance warning: more than 24
--   queries, more than 3 merge queries, or a refresh interval under 900 seconds.
--   Clean dashboards are excluded. For a full tile count across the estate,
--   remove the HAVING clause.
--
-- * LIMIT 100 truncates. Estates larger than 100 flagged dashboards will be cut
--   off silently. Raise or remove it.
--
-- * Deleted dashboards are already excluded via the DELETED_AT filter.
--
-- COLUMNS RETURNED
--   user.name                 dashboard owner
--   dashboard.is_legacy       'Yes' if not using dashboards-next
--   dashboard.id              Looker dashboard id
--   dashboard.title           display name, which is what the business uses and
--                             will often NOT match the LookML file name
--   dashboard_element.count   tile count, the number you multiply for sizing

SELECT
    `user.name`,
    `dashboard.is_legacy`,
    `dashboard.id`,
    `dashboard.title`,
    `dashboard_element.count`
FROM
    (SELECT
            dashboard.id AS `dashboard.id`,
            dashboard.TITLE  AS `dashboard.title`,
                (CASE WHEN (dashboard.preferred_viewer != 'dashboards-next') OR (dashboard.preferred_viewer IS NULL)  THEN 'Yes' ELSE 'No' END) AS `dashboard.is_legacy`,
            CONCAT((CONVERT(user.first_name USING utf8mb4)), ' ', (CONVERT(user.last_name USING utf8mb4)))  AS `user.name`,
            COUNT(DISTINCT dashboard_element.id) AS `dashboard_element.count`,
            COUNT(DISTINCT query.id) AS `query.count`,
            COUNT(DISTINCT CASE WHEN ((( CASE
WHEN dashboard_element.look_id IS NOT NULL  THEN 'Saved Look'
WHEN dashboard_element.query_id IS NOT NULL OR result_maker.query_id IS NOT NULL  THEN 'Lookless'
WHEN dashboard_element.result_maker_id IS NOT NULL  THEN 'Merge Query'
ELSE 'No Result'
END )) = 'Merge Query') THEN dashboard_element.id ELSE NULL END) AS `dashboard_element.count_merge_query`,
            MIN(900 / (
            CASE WHEN dashboard.refresh_interval LIKE '%second%' THEN CAST(SUBSTRING_INDEX(dashboard.refresh_interval, ' ', 1) AS UNSIGNED)
              WHEN dashboard.refresh_interval LIKE '%minute%' THEN CAST(SUBSTRING_INDEX(dashboard.refresh_interval, ' ', 1) AS UNSIGNED)*60
              WHEN dashboard.refresh_interval LIKE '%hour%' THEN CAST(SUBSTRING_INDEX(dashboard.refresh_interval, ' ', 1) AS UNSIGNED)*60*60
              WHEN dashboard.refresh_interval LIKE '%day%' THEN CAST(SUBSTRING_INDEX(dashboard.refresh_interval, ' ', 1) AS UNSIGNED)*24*60*60
              ELSE CAST(SUBSTRING_INDEX(dashboard.refresh_interval, ' ', 1) AS UNSIGNED)
            END
           )) AS `dashboard_element.sorted_warnings_0_0`
        FROM dashboard_layout_component
LEFT JOIN dashboard_layout ON dashboard_layout_component.dashboard_layout_id = dashboard_layout.id
LEFT JOIN dashboard_element ON dashboard_layout_component.dashboard_element_id = dashboard_element.id
LEFT JOIN result_maker  AS result_maker ON dashboard_element.result_maker_id = result_maker.id
LEFT JOIN look ON dashboard_element.look_id = look.id
LEFT JOIN merge_query ON result_maker.merge_query_id = merge_query.id
LEFT JOIN merge_query_source_query ON merge_query.id = merge_query_source_query.merge_query_id
LEFT JOIN query ON COALESCE(result_maker.query_id, dashboard_element.query_id, look.query_id, merge_query_source_query.query_id) = query.id
LEFT JOIN dashboard ON dashboard_layout.dashboard_id = dashboard.id
LEFT JOIN user  AS user ON dashboard.user_id = user.id
        WHERE NOT (((dashboard.DELETED_AT) IS NOT NULL) OR ((dashboard.id) IS NULL AND (dashboard.DELETED_AT) IS NULL) ) OR (((dashboard.DELETED_AT) IS NOT NULL) OR ((dashboard.id) IS NULL AND (dashboard.DELETED_AT) IS NULL) ) IS NULL
        GROUP BY
            1,
            2,
            3,
            4
        HAVING COALESCE((`query.count` - 24) * (FLOOR(`query.count` / 24) / NULLIF(FLOOR(`query.count` / 24), 0)), 0) + COALESCE((`dashboard_element.count_merge_query` - 3) * (FLOOR(`dashboard_element.count_merge_query` / 3) / NULLIF(FLOOR(`dashboard_element.count_merge_query` / 3), 0)), 0) + COALESCE(FLOOR(`dashboard_element.sorted_warnings_0_0`), 0) > 0) AS `t3`
ORDER BY
    `dashboard_element.count` DESC
LIMIT 100