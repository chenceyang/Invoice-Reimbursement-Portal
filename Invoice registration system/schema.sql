CREATE DATABASE IF NOT EXISTS invoice_system DEFAULT CHARACTER SET utf8mb4;
USE invoice_system;

SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS reimburse_records;
DROP TABLE IF EXISTS reimburse_items;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS invoice_types;
DROP TABLE IF EXISTS users;

SET FOREIGN_KEY_CHECKS = 1;

-- =========================
-- 1. 用户表
-- =========================
CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    employee_no VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    password VARCHAR(255) NOT NULL,
    role ENUM('employee','admin') NOT NULL DEFAULT 'employee',
    total_quota DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '当前可核销额度（审核通过未核销金额）',
    used_quota DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '累计已核销金额',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =========================
-- 2. 发票类型配置表
-- 员工提交发票时选择
-- =========================
CREATE TABLE invoice_types (
    id INT AUTO_INCREMENT PRIMARY KEY,
    type_name VARCHAR(255) NOT NULL UNIQUE,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =========================
-- 3. 发票主表
-- 状态流：
-- Pending   待审核
-- Approved  审核通过（计入员工额度）
-- Rejected  审核驳回
-- Reimbursed 已核销
-- =========================
CREATE TABLE invoices (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    receipt_date DATE NULL COMMENT '收票日期',
    submitter_name VARCHAR(100) COMMENT '交票人',
    pdf_filename VARCHAR(255) COMMENT '原始上传文件名',
    invoice_date DATE NULL COMMENT '开票日期',
    invoice_no VARCHAR(100) NOT NULL COMMENT '发票号码',
    buyer_name VARCHAR(255) COMMENT '购买方名称',
    seller_name VARCHAR(255) COMMENT '销售方名称',
    amount DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '价税合计小写金额',
    project_name VARCHAR(255) COMMENT '项目名称',
    invoice_type_id INT NOT NULL COMMENT '发票类型',
    file_path VARCHAR(255) COMMENT '服务器保存的唯一文件名',
    status ENUM('Pending','Approved','Rejected','Reimbursed') NOT NULL DEFAULT 'Pending',
    finance_comment VARCHAR(255) COMMENT '财务备注/审核备注',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uk_invoice_no (invoice_no),
    KEY idx_invoice_user (user_id),
    KEY idx_invoice_status (status),
    KEY idx_invoice_date (invoice_date),
    KEY idx_invoice_type (invoice_type_id),

    CONSTRAINT fk_invoice_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_invoice_type FOREIGN KEY (invoice_type_id) REFERENCES invoice_types(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =========================
-- 4. 核销事项配置表
-- 财务核销弹窗下拉框
-- =========================
CREATE TABLE reimburse_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    item_name VARCHAR(255) NOT NULL UNIQUE,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =========================
-- 5. 核销记录表
-- 财务每次核销都记录一条
-- =========================
CREATE TABLE reimburse_records (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL COMMENT '被核销员工',
    reimburse_item_id INT NOT NULL COMMENT '核销事项',
    amount DECIMAL(12,2) NOT NULL DEFAULT 0.00 COMMENT '核销金额',
    comment VARCHAR(255) COMMENT '备注',
    created_by INT NOT NULL COMMENT '操作财务管理员ID',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    KEY idx_rr_user (user_id),
    KEY idx_rr_item (reimburse_item_id),
    KEY idx_rr_created_by (created_by),

    CONSTRAINT fk_rr_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_rr_item FOREIGN KEY (reimburse_item_id) REFERENCES reimburse_items(id),
    CONSTRAINT fk_rr_admin FOREIGN KEY (created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =========================
-- 6. 初始用户
-- =========================
INSERT INTO users (employee_no, name, password, role, total_quota, used_quota) VALUES
('E1001','Ceyang Chen','scrypt:32768:8:1$demo$48ff5c63a3fbe4ca7aaf238b8576b7937ea4e6773f4e9f08b1e99f922d72d90e19a1dfe7c29bb90fd3677e6a4f7a30ef6e595d4ebdf1dc2c72c95c11ebd8e58d','employee',0,0),
('E1002','Ronghao Lin','scrypt:32768:8:1$demo$48ff5c63a3fbe4ca7aaf238b8576b7937ea4e6773f4e9f08b1e99f922d72d90e19a1dfe7c29bb90fd3677e6a4f7a30ef6e595d4ebdf1dc2c72c95c11ebd8e58d','employee',0,0),
('A0001','Finance Admin','scrypt:32768:8:1$demo$48ff5c63a3fbe4ca7aaf238b8576b7937ea4e6773f4e9f08b1e99f922d72d90e19a1dfe7c29bb90fd3677e6a4f7a30ef6e595d4ebdf1dc2c72c95c11ebd8e58d','admin',0,0);

-- =========================
-- 7. 发票类型配置（可继续扩）
-- =========================
INSERT INTO invoice_types (type_name, is_active) VALUES
('差旅费', 1),
('交通费', 1),
('办公用品', 1),
('餐饮费', 1),
('家具家电', 1),
('通讯费', 1),
('油费', 1),
('住宿费', 1),
('培训费', 1),
('会务费', 1),
('快递物流费', 1),
('维修维护费', 1),
('软件服务费', 1),
('其他', 1);

-- =========================
-- 8. 核销事项配置（财务端下拉）
-- =========================
INSERT INTO reimburse_items (item_name, is_active) VALUES
('差旅报销', 1),
('交通报销', 1),
('餐饮报销', 1),
('办公采购核销', 1),
('家具家电采购核销', 1),
('油费核销', 1),
('住宿报销', 1),
('通讯费报销', 1),
('培训费用核销', 1),
('其他核销', 1);