-- Adds the column that records which model wrote a summary. Safe to run more than once.
-- Older rows keep NULL (unknown); the pipeline stores the model name from now on.
ALTER TABLE updates ADD COLUMN IF NOT EXISTS summary_model TEXT;
