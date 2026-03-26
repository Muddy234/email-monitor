ALTER TABLE profiles ADD COLUMN IF NOT EXISTS pipeline_stage TEXT DEFAULT 'idle';
