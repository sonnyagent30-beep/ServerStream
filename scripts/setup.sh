#!/bin/bash
set -e

echo "=== ServerStream Setup ==="

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (use sudo)"
    exit 1
fi

if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo "Docker installed."
else
    echo "Docker already installed."
fi

PROJECT_DIR="/opt/serversstream"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

if [ -d ".git" ]; then
    echo "Updating repo..."
    git pull
else
    echo "Cloning ServerStream repo..."
    git clone https://github.com/sonnyagent30-beep/ServerStream.git .
fi

if [ ! -f ".env" ]; then
    echo "Creating .env file..."
    cat > .env << 'EOF'
CANDIDATE=your-server-ip-or-domain
STREAM_KEY=change-me-to-a-random-string
EOF
    echo "Created .env — edit it to set your stream key and domain!"
fi

echo "Configuring firewall..."
ufw allow 1935/tcp comment 'ServerStream RTMP'
ufw allow 1985/tcp comment 'ServerStream API'
ufw allow 8080/tcp comment 'ServerStream HLS'
ufw allow 8081/tcp comment 'ServerStream Web'

echo "Starting ServerStream..."
docker compose up -d

echo ""
echo "=== ServerStream deployed! ==="
echo "Dashboard: http://$(curl -s ifconfig.me):8081"
echo "SRS API:   http://$(curl -s ifconfig.me):1985/api/v1/versions"
echo ""
echo "Next steps:"
echo "1. Edit .env to set your stream key: nano $PROJECT_DIR/.env"
echo "2. Edit srs.conf to add platform push URLs"
echo "3. Restart: docker compose restart"
echo ""
