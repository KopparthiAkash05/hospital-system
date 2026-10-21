-- ===========================================================================
-- MediCare Hospital — Live queue management migration
--
-- Adds only the tables required by the live queue feature:
--   * QueueEntry   (one row per queue position: approved appointment or
--                   walk-in patient, numbered uniquely per doctor per day)
--   * QueueSequence(per doctor per day atomic counter used to allocate
--                   queue numbers without MAX()+1 races)
--
-- Numbering policy: each doctor gets an independent sequence that resets
-- every day (one queue session per doctor per date). Queue numbers are
-- allocated only after the normal approval rules have been satisfied, so
-- pending or rejected bookings never consume a number.
--
-- Every statement is guarded so re-running this file is a no-op.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. QueueEntry
--    appointment_id is nullable for walk-in patients; queue numbers are
--    unique per doctor per day, enforced at the database level.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS QueueEntry (
    queue_id INT AUTO_INCREMENT PRIMARY KEY,
    doctor_id INT NOT NULL,
    queue_date DATE NOT NULL,
    queue_number INT NOT NULL,
    appointment_id INT DEFAULT NULL,
    patient_id INT DEFAULT NULL,
    walk_in_name VARCHAR(150) DEFAULT NULL,
    walk_in_phone VARCHAR(30) DEFAULT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Waiting',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME DEFAULT NULL,
    completed_at DATETIME DEFAULT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_queue_number (doctor_id, queue_date, queue_number),
    KEY idx_queue_appointment (appointment_id),
    KEY idx_queue_doctor_date (doctor_id, queue_date, status),
    KEY idx_queue_patient (patient_id),
    FOREIGN KEY (doctor_id) REFERENCES Doctor(doctor_id) ON DELETE CASCADE,
    FOREIGN KEY (appointment_id) REFERENCES Appointment(appointment_id) ON DELETE CASCADE,
    FOREIGN KEY (patient_id) REFERENCES Patient(patient_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 2. QueueSequence
--    The PRIMARY KEY row for (doctor_id, queue_date) is locked by the
--    allocation UPDATE, which serialises concurrent number allocation for
--    the same queue session. last_number only ever increases inside a
--    session, so the daily reset is implicit (a fresh row per date).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS QueueSequence (
    doctor_id INT NOT NULL,
    queue_date DATE NOT NULL,
    last_number INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id, queue_date),
    FOREIGN KEY (doctor_id) REFERENCES Doctor(doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 3. Compatibility guards: bring an existing compatible QueueEntry table up
--    to the full column set used by the application.
-- ---------------------------------------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'QueueEntry'
             AND COLUMN_NAME = 'walk_in_phone');
SET @s := IF(@c = 0, 'ALTER TABLE QueueEntry ADD COLUMN walk_in_phone VARCHAR(30) DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'QueueEntry'
             AND COLUMN_NAME = 'completed_at');
SET @s := IF(@c = 0, 'ALTER TABLE QueueEntry ADD COLUMN completed_at DATETIME DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
