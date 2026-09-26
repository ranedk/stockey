CREATE DATABASE systrader;
CREATE USER systrader WITH ENCRYPTED PASSWORD 'systrader';
GRANT ALL PRIVILEGES ON DATABASE systrader TO systrader;
ALTER DATABASE systrader OWNER TO systrader;
\c systrader
GRANT ALL ON SCHEMA public TO systrader;
GRANT USAGE ON SCHEMA public TO systrader;
CREATE EXTENSION IF NOT EXISTS timescaledb;

