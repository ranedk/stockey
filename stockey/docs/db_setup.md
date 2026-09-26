# Setting up the server

To set up a secure DigitalOcean server running **PostgreSQL** and **Redis**, accessible **only via your ZeroTier network**, and to lock everything else down using **UFW (Uncomplicated Firewall)**, here’s a **step-by-step plan**.

## ✅ Step 1: Create and Configure the Droplet

1. **Create a droplet** on DigitalOcean (Ubuntu 22.04 LTS recommended).
2. Enable SSH key access and **login via SSH**:

   ```bash
   ssh root@your_droplet_ip
   ```

## ✅ Step 2: Install ZeroTier

1. Install ZeroTier:

   ```bash
   curl -s https://install.zerotier.com | sudo bash
   sudo zerotier-cli join <your_zerotier_network_id>
   ```

2. Get the assigned ZeroTier IP:

   ```bash
   ip addr show | grep -A 3 -B 3 zt
   ```

3. Go to [https://my.zerotier.com](https://my.zerotier.com) and **authorize** the machine in the network members section.


## ✅ Step 3: Install PostgreSQL and Redis

```bash
# Update first
sudo apt update && sudo apt upgrade -y

# PostgreSQL
sudo apt install postgresql postgresql-contrib -y

# Redis
sudo apt install redis -y
```


## ✅ Step 4: Bind PostgreSQL and Redis to ZeroTier Interface

### 🔧 PostgreSQL Config

1. Edit `postgresql.conf`:

   ```bash
   sudo nano /etc/postgresql/*/main/postgresql.conf
   ```

   Find `listen_addresses` and change:

   ```
   listen_addresses = '127.0.0.1,<zerotier_ip>'
   ```

2. Edit `pg_hba.conf`:

   ```bash
   sudo nano /etc/postgresql/*/main/pg_hba.conf
   ```

   Add:

   ```
   host    all             all             <zerotier-subnet>            md5
   ```

   The `zerotier-subnet` is available against your network ID on the website and looks like "172.26.0.0/16"

3. Restart PostgreSQL:

   ```bash
   sudo systemctl restart postgresql
   ```

### 🔧 Redis Config

1. Edit `redis.conf`:

   ```bash
   sudo nano /etc/redis/redis.conf
   ```

   Change:

   ```
   bind 127.0.0.1 <zerotier_ip>     # replace the line which binds to 127.0.0.1, -::1
   protected-mode no                # This is ONLY SAFE if ufw blocks everything and connections are made only using zerotier network
   ```

2. Restart Redis:

   ```bash
   sudo systemctl restart redis
   ```

---

## ✅ Step 5: Configure UFW

1. **Enable UFW** and default deny:

   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   ```

2. **Allow SSH from anywhere** (or restrict to specific IPs if you prefer):

   ```bash
   sudo ufw allow ssh
   ```

3. **Allow only ZeroTier interface traffic**:
   Find your ZeroTier interface name:

   ```bash
   ip a | grep zt
   ```

   Suppose it is `ztxxxxxx`.

4. Allow all traffic **on ZeroTier interface only**:

   ```bash
   sudo ufw allow in on ztxxxxxx
   ```

5. **Enable UFW**:

   ```bash
   sudo ufw enable
   ```

6. Check status:

   ```bash
   sudo ufw status verbose
   ```

## ✅ Step 6: (Optional) Harden the Server


* Disable root login and password authentication in SSH config:

  ```bash
  sudo nano /etc/ssh/sshd_config
  # Set:
  PermitRootLogin no
  PasswordAuthentication no
  ```

  ```bash
  sudo systemctl restart ssh
  ```

# Move database disk files to external volume

To ensure **data durability** for both Redis and PostgreSQL by relocating their data directories to `/mnt/database/redis` and `/mnt/database/postgres` respectively, follow these **safe and clean steps** below. These are applicable since your installations are new and empty.


## ✅ Step 1: Prepare the Directories

Make sure the destination mount exists and is owned by the correct users.

```bash
# Create directories
sudo mkdir -p /mnt/database/redis
sudo mkdir -p /mnt/database/postgres

# Set correct ownerships (redis:redis and postgres:postgres)
sudo chown redis:redis -R /mnt/database/redis
sudo chown postgres:postgres -R /mnt/database/postgres

# Permissions
sudo chmod 711 /mnt
sudo chmod 751 /mnt/database
sudo chmod 750 -R /mnt/database/redis /mnt/database/postgres
```

---

## ✅ Step 2: Move Redis Data to `/mnt/database/redis`

1. Stop Redis:

   ```bash
   sudo systemctl stop redis
   ```

2. Edit Redis config:

   ```bash
   sudo nano /etc/redis/redis.conf
   ```

   Change this line (or add if missing):

   ```
   dir /mnt/database/redis
   ```

3. Move old dump (optional, if exists):

   ```bash
   sudo mv /var/lib/redis/dump.rdb /mnt/database/redis/ 2>/dev/null || true
   ```

4. Restart Redis:

   ```bash
   sudo systemctl restart redis
   ```

5. Verify Redis is writing to the new directory:

   ```bash
   redis-cli CONFIG GET dir
   ```

---

## ✅ Step 3: Move PostgreSQL 16 Data to `/mnt/database/postgres`

1. Stop PostgreSQL:

   ```bash
   sudo systemctl stop postgresql
   ```

2. Confirm current version and location:

   ```bash
   pg_lsclusters
   ```

   Expected:

   ```
   Ver Cluster Port Status Owner    Data directory              Log file
   16  main    5432  down   postgres /var/lib/postgresql/16/main ...
   ```

3. Move the cluster directory:

   ```bash
   sudo mv /var/lib/postgresql/16/main /mnt/database/postgres/
   ```

4. Update cluster config:

   ```bash
   sudo pg_conftool 16 main set data_directory '/mnt/database/postgres/main'
   ```

   Or manually edit:

   ```bash
   sudo nano /etc/postgresql/16/main/postgresql.conf
   ```

   Set:

   ```
   data_directory = '/mnt/database/postgres/main'
   ```

5. Set ownership (critical):

   ```bash
   sudo chown -R postgres:postgres /mnt/database/postgres
   sudo chmod 700 /mnt/database/postgres/main
   ```

6. Restart PostgreSQL:

   ```bash
   sudo systemctl start postgresql
   ```

7. Verify new data directory:

   ```bash
   sudo -u postgres psql -c "SHOW data_directory;"
   ```
