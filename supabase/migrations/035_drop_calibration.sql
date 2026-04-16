-- Drop calibration system: tables, columns, indexes, and policies added in
-- 034_calibration.sql. The post-onboarding calibration loop has been removed
-- from the worker; these objects are no longer read or written.

DROP POLICY IF EXISTS "Users can view own calibration results" ON calibration_results;
DROP INDEX IF EXISTS idx_calibration_results_user_iteration;
DROP TABLE IF EXISTS calibration_results;

ALTER TABLE profiles
  DROP COLUMN IF EXISTS calibration_status,
  DROP COLUMN IF EXISTS calibration_rules,
  DROP COLUMN IF EXISTS calibration_iteration,
  DROP COLUMN IF EXISTS calibration_retry_count,
  DROP COLUMN IF EXISTS calibrated_at;
