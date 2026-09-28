-- Databases and users for the local Stroom stack (development only).
CREATE DATABASE IF NOT EXISTS stroom CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS stats CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'stroomuser'@'%' IDENTIFIED BY 'stroompassword1';
CREATE USER IF NOT EXISTS 'statsuser'@'%' IDENTIFIED BY 'stroompassword1';
GRANT ALL PRIVILEGES ON stroom.* TO 'stroomuser'@'%';
GRANT ALL PRIVILEGES ON stats.* TO 'statsuser'@'%';
FLUSH PRIVILEGES;
