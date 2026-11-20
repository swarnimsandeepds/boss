-- Run once in the SingleStore SQL editor.
CREATE DATABASE IF NOT EXISTS boss;
USE boss;

CREATE TABLE IF NOT EXISTS events (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id     VARCHAR(32)  NOT NULL,
  step       VARCHAR(40)  NOT NULL,
  actor      VARCHAR(40)  NOT NULL,
  payload    JSON         NOT NULL,
  created_at DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY (run_id),
  KEY (step, actor)
);
