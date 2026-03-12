# EC2 launch guide for `src/strategy/paper_runner.py`

This runner only uses Gemini public REST endpoints and the local `data/deribit/bates_params_implied.json` file. It does not require Gemini API keys.

## Recommended instance

- AMI: Ubuntu Server 24.04 LTS
- Architecture: `x86_64`
- Instance type: `t3.small`
- Storage: `20 GB` gp3

This is a light polling process with a small local C++ extension build. `t3.small` is enough unless you plan to run calibration jobs on the same box.

## Launch in AWS

1. In the EC2 console, launch an Ubuntu 24.04 LTS instance.
2. If you want shell access through AWS Systems Manager instead of SSH, attach an IAM role with `AmazonSSMManagedInstanceCore`.
3. If you want SSH or EC2 Instance Connect, allow inbound TCP `22` only from your IP in the instance security group.

Official AWS references:

- EC2 launch: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EC2_GetStarted.html>
- Connect to Linux instances: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/connect-to-linux-instance.html>
- Security groups: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-security-groups.html>
- Session Manager: <https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html>

## Provision the instance

Run these commands after connecting:

```bash
sudo apt update
sudo apt install -y git python3-venv python3-dev build-essential
cd /home/ubuntu
git clone <your-repo-url> gtc-2
cd /home/ubuntu/gtc-2
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-paper.txt
bash src/pricer/build.sh
mkdir -p logs
cp config/paper.ec2.example.json config/paper.ec2.json
python src/strategy/paper_runner.py --once --config config/paper.ec2.json
```

Expected result from the one-shot run:

- The process prints `Loaded params from ...`
- It scans active events without import/build errors
- It creates or updates `data/strategy/positions.ec2.json` and `data/strategy/paper_trades.ec2.csv`

## Run it as a service

The included unit file assumes the repo lives at `/home/ubuntu/gtc-2` and runs as the `ubuntu` user.

```bash
sudo cp deploy/paper-runner.service /etc/systemd/system/paper-runner.service
sudo systemctl daemon-reload
sudo systemctl enable --now paper-runner
sudo systemctl status paper-runner
journalctl -u paper-runner -f
```

## Common operational notes

- The current repo includes a macOS-built `src/pricer/bates_pricer.cpython-312-darwin.so`. That file is not usable on EC2. `bash src/pricer/build.sh` builds the Linux version in place.
- If you change the repo path, username, or config filename, update `deploy/paper-runner.service` before copying it into `/etc/systemd/system/`.
- The runner now tolerates transient network failures in service mode and retries on the next poll cycle. `--once` still exits non-zero on real errors so you can verify setup cleanly.
