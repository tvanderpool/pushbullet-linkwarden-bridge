# Pushbullet to Linkwarden Bridge

A Python application that automatically saves links pushed to Pushbullet devices into organized Linkwarden collections. Perfect for saving articles, bookmarks, and interesting links from your phone or any device directly to your self-hosted Linkwarden instance.

## Features

- **Real-time synchronization**: Listens to Pushbullet's WebSocket stream for instant link detection
- **Multi-device support**: Configure different Linkwarden collections for different devices
- **Auto-configuration**: Automatically creates missing devices and collections
- **Persistent tracking**: Remembers processed pushes to avoid duplicates
- **Resilient**: Automatic reconnection with exponential backoff
- **Dry-run mode**: Test your configuration without creating actual links
- **Graceful shutdown**: Handles SIGINT and SIGTERM signals properly
- **Comprehensive logging**: Structured logging with configurable levels

## Prerequisites

- Python 3.10 or higher
- A [Pushbullet](https://www.pushbullet.com/) account with API access
- A [Linkwarden](https://linkwarden.app/) instance (self-hosted or cloud)
- API tokens for both services

## Installation

1. **Clone this repository**:
   ```bash
   git clone <repository-url>
   cd pushbullet-linkwarden-bridge
   ```

2. **Create a virtual environment** (recommended):
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Create your configuration file**:
   ```bash
   cp config.example.yaml config.yaml
   ```

5. **Edit `config.yaml`** with your credentials and preferences (see Configuration section below)

## Configuration

### Getting API Tokens

#### Pushbullet API Token
1. Go to [Pushbullet Settings](https://www.pushbullet.com/#settings/account)
2. Scroll to "Access Tokens"
3. Click "Create Access Token"
4. Copy the token to your `config.yaml`

#### Linkwarden API Token
1. Log into your Linkwarden instance
2. Go to Settings → API
3. Create a new API token
4. Copy the token to your `config.yaml`

### Configuration File

Edit `config.yaml`:

```yaml
# Linkwarden API Configuration
linkwarden:
  api_url: "https://your-linkwarden-instance.com"  # Your Linkwarden URL
  api_token: "your-linkwarden-api-token"           # Your API token

# Pushbullet API Configuration
pushbullet:
  api_token: "your-pushbullet-api-token"           # Your API token

# Channel mappings
channels:
  - name: "My Phone"              # Pushbullet device name
    collection: "Personal Links"  # Linkwarden collection name
    collection_id: null           # Auto-filled on first run
    device_iden: null             # Auto-filled on first run

  - name: "Work Computer"
    collection: "Work Resources"
    collection_id: null
    device_iden: null

# Optional settings
settings:
  dry_run: false                  # Set to true to test without creating links
  log_level: "INFO"              # DEBUG, INFO, WARNING, ERROR
  reconnect_max_delay: 300       # Max seconds between reconnection attempts
  reconnect_initial_delay: 2     # Initial delay in seconds
  request_timeout: 30            # API request timeout in seconds
```

### Channel Configuration

Each channel maps a Pushbullet device to a Linkwarden collection:

- **name**: The nickname of your Pushbullet device (e.g., "My Phone", "iPad")
- **collection**: The name of the Linkwarden collection where links should be saved
- **collection_id**: Automatically filled on first run (leave as `null`)
- **device_iden**: Automatically filled on first run (leave as `null`)

**Note**: The application will automatically:
- Create Pushbullet devices if they don't exist
- Create Linkwarden collections if they don't exist
- Update the config file with the resolved IDs

## Usage

### Running the Bridge

Start the application:

```bash
python main.py
```

Or make it executable:

```bash
chmod +x main.py
./main.py
```

### Testing with Dry-Run Mode

Before running in production, test your configuration:

1. Set `dry_run: true` in `config.yaml`
2. Run the application
3. Push a link from Pushbullet
4. Check the logs to verify it would be saved correctly
5. Set `dry_run: false` when ready

### Sending Links from Pushbullet

#### From Mobile
1. Share a link from any app
2. Choose "Pushbullet"
3. Select the device configured in your channels
4. The link will automatically appear in your Linkwarden collection

#### From Browser Extension
1. Install the Pushbullet browser extension
2. Click the extension icon on any page
3. Select "Push Link"
4. Choose the target device
5. The link will be saved to the corresponding collection

## How It Works

### Startup Sequence

1. **Load Configuration**: Reads `config.yaml`
2. **Validate Settings**: Ensures all required fields are present
3. **Resolve Devices**: Matches device names to Pushbullet device IDs
   - Creates missing devices automatically
4. **Resolve Collections**: Matches collection names to Linkwarden collection IDs
   - Creates missing collections automatically
5. **Update Config**: Saves resolved IDs back to `config.yaml`
6. **Load History**: Reads `processed_pushes.json` to avoid duplicates
7. **Process Backlog**: Handles any new pushes since last run
   - For existing devices: processes new pushes
   - For new devices: records state without processing old pushes
8. **Connect to Stream**: Opens WebSocket connection to Pushbullet
9. **Main Loop**: Listens for new pushes in real-time

### Push Processing

1. Pushbullet sends a "tickle" message when a new push arrives
2. The bridge fetches recent pushes via the REST API
3. Filters for `type="link"` pushes only
4. Checks if the push's device is configured in a channel
5. Verifies the push hasn't been processed before
6. Creates a link in the corresponding Linkwarden collection
7. Updates the processed pushes history

### Duplicate Prevention

The bridge maintains a `processed_pushes.json` file that tracks:
- Device identifier
- Last processed push timestamp

This ensures:
- No duplicate links are created
- Pushes are processed exactly once
- The bridge can resume after restarts without reprocessing

## Running as a Service

### systemd (Linux)

Create `/etc/systemd/system/pushbullet-linkwarden.service`:

```ini
[Unit]
Description=Pushbullet to Linkwarden Bridge
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/pushbullet-linkwarden-bridge
Environment="PATH=/path/to/pushbullet-linkwarden-bridge/venv/bin"
ExecStart=/path/to/pushbullet-linkwarden-bridge/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable pushbullet-linkwarden
sudo systemctl start pushbullet-linkwarden
sudo systemctl status pushbullet-linkwarden
```

View logs:

```bash
sudo journalctl -u pushbullet-linkwarden -f
```

### Docker

Create a `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["python", "main.py"]
```

Create a `docker-compose.yml`:

```yaml
version: '3.8'

services:
  bridge:
    build: .
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./processed_pushes.json:/app/processed_pushes.json
    restart: unless-stopped
```

Run:

```bash
docker-compose up -d
```

## Troubleshooting

### Common Issues

#### "Configuration file not found"
- Ensure `config.yaml` exists in the same directory as `main.py`
- Copy `config.example.yaml` to `config.yaml` if needed

#### "Missing linkwarden.api_token"
- Verify all required fields in `config.yaml` are filled
- Check for typos in field names

#### "Linkwarden API error (POST links): 401"
- Verify your Linkwarden API token is correct
- Check if the token has necessary permissions
- Ensure the token hasn't expired

#### "WebSocket connection failed"
- Check your internet connection
- Verify your Pushbullet API token is valid
- Check if Pushbullet's service is operational

#### Links not being detected
- Ensure you're pushing to a device configured in your channels
- Check the logs for any errors
- Verify the push type is "link" (not "note" or "file")
- Run in dry-run mode to see what's being detected

#### Duplicate links being created
- Check if `processed_pushes.json` exists and is writable
- Ensure the file isn't being deleted between runs
- Verify file permissions

### Debug Mode

Enable debug logging in `config.yaml`:

```yaml
settings:
  log_level: "DEBUG"
```

This will show detailed information about:
- WebSocket messages
- API requests and responses
- Push processing decisions
- Device and collection matching

### Testing API Connectivity

Test Pushbullet API:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" https://api.pushbullet.com/v2/devices
```

Test Linkwarden API:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" https://your-linkwarden-instance.com/api/v1/collections
```

## Files

- **main.py**: Main application code
- **config.yaml**: Your configuration (not committed to git)
- **config.example.yaml**: Example configuration template
- **requirements.txt**: Python dependencies
- **processed_pushes.json**: Push history (created automatically)
- **.gitignore**: Git ignore rules

## Security Considerations

- **API Tokens**: Never commit `config.yaml` to version control (it's in `.gitignore`)
- **File Permissions**: Ensure `config.yaml` is readable only by the user running the application
- **Network**: The bridge makes outbound HTTPS connections only
- **Data**: Processed push data is stored locally in `processed_pushes.json`

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

MIT License - see LICENSE file for details

## Support

For issues and questions:
- Check the Troubleshooting section above
- Review application logs
- Open an issue on GitHub

## Acknowledgments

- [Pushbullet](https://www.pushbullet.com/) for their excellent API
- [Linkwarden](https://linkwarden.app/) for the bookmark management platform
