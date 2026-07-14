-- Invoice registration system schema (MySQL 8.0+)
-- This script creates/updates structural objects only. It does not delete or
-- overwrite employee, invoice, or reimbursement data.

CREATE DATABASE IF NOT EXISTS `invoice_system`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE `invoice_system`;

CREATE TABLE IF NOT EXISTS `schema_migrations` (
  `version` VARCHAR(50) NOT NULL,
  `applied_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `users` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `employee_no` VARCHAR(50) NOT NULL,
  `name` VARCHAR(100) NOT NULL,
  `password` VARCHAR(255) NOT NULL,
  `role` ENUM('employee','admin') NOT NULL DEFAULT 'employee',
  `total_quota` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  `used_quota` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  `created_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_users_employee_no` (`employee_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `invoice_types` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `type_name` VARCHAR(255) NOT NULL,
  `is_active` TINYINT(1) NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_invoice_types_name` (`type_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `invoices` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT NOT NULL,
  `receipt_date` DATE DEFAULT NULL,
  `submitter_name` VARCHAR(100) DEFAULT NULL,
  `pdf_filename` VARCHAR(255) DEFAULT NULL,
  `invoice_date` DATE DEFAULT NULL,
  `invoice_no` VARCHAR(100) NOT NULL,
  `buyer_name` VARCHAR(255) DEFAULT NULL,
  `buyer_tax_no` VARCHAR(50) DEFAULT NULL,
  `seller_name` VARCHAR(255) DEFAULT NULL,
  `seller_tax_no` VARCHAR(50) DEFAULT NULL,
  `amount` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  `project_name` VARCHAR(255) DEFAULT NULL,
  `invoice_type_id` INT NOT NULL,
  `file_path` VARCHAR(255) DEFAULT NULL,
  `status` ENUM('Pending','Approved','Rejected','Reimbursed') NOT NULL DEFAULT 'Pending',
  `approved_amount` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  `finance_comment` VARCHAR(255) DEFAULT NULL,
  `created_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_invoices_invoice_no` (`invoice_no`),
  KEY `idx_invoices_user_id` (`user_id`),
  KEY `idx_invoices_type_id` (`invoice_type_id`),
  CONSTRAINT `fk_invoices_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_invoices_type` FOREIGN KEY (`invoice_type_id`) REFERENCES `invoice_types` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `reimburse_items` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `item_name` VARCHAR(100) NOT NULL,
  `is_active` TINYINT(1) NOT NULL DEFAULT 1,
  `created_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `reimbursement_logs` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `invoice_id` INT NOT NULL,
  `user_id` INT NOT NULL,
  `reimburse_item_id` INT NOT NULL,
  `amount` DECIMAL(12,2) NOT NULL,
  `comment` TEXT DEFAULT NULL,
  `created_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_invoice_id` (`invoice_id`),
  KEY `idx_user_id` (`user_id`),
  KEY `idx_reimburse_item_id` (`reimburse_item_id`),
  CONSTRAINT `fk_reimbursement_logs_invoice` FOREIGN KEY (`invoice_id`) REFERENCES `invoices` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_reimbursement_logs_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_reimbursement_logs_item` FOREIGN KEY (`reimburse_item_id`) REFERENCES `reimburse_items` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO `invoice_types` (`type_name`, `is_active`) VALUES
  ('出租车发票', 1),
  ('定额发票', 1),
  ('火车票', 1),
  ('过路费', 1)
ON DUPLICATE KEY UPDATE `is_active` = VALUES(`is_active`);

INSERT INTO `reimburse_items` (`item_name`, `is_active`)
SELECT '日常报销', 1
WHERE NOT EXISTS (SELECT 1 FROM `reimburse_items` WHERE `item_name` = '日常报销');

INSERT INTO `reimburse_items` (`item_name`, `is_active`)
SELECT '差旅报销', 1
WHERE NOT EXISTS (SELECT 1 FROM `reimburse_items` WHERE `item_name` = '差旅报销');

INSERT INTO `reimburse_items` (`item_name`, `is_active`)
SELECT '项目报销', 1
WHERE NOT EXISTS (SELECT 1 FROM `reimburse_items` WHERE `item_name` = '项目报销');

INSERT IGNORE INTO `schema_migrations` (`version`) VALUES ('20260714_current_schema');

