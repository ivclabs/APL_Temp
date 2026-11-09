# APL_Temp on AWS: one EC2 server with a fixed IP, running the Flask app behind nginx.

# Newest Amazon Linux 2023 image, looked up automatically (no hard-coded AMI id).
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# ---------- Firewall ----------

resource "aws_security_group" "web" {
  name        = "apl-temp-web"
  description = "APL_Temp: web open to all, SSH only from home"

  ingress {
    description = "Web page + ESP32 readings"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "SSH from home only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  egress {
    description = "Server may reach the internet (updates, GitHub, AWS APIs)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------- Secret API key ----------

# Stored encrypted in Parameter Store, so it is never in GitHub or in the server's startup script.
resource "aws_ssm_parameter" "api_key" {
  name  = "/apl-temp/api_key"
  type  = "SecureString"
  value = var.api_key
}

# ---------- Permissions for the server (IAM role) ----------

# The server gets permissions through a role instead of stored access keys.
resource "aws_iam_role" "server" {
  name = "apl-temp-server"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Only what it needs: read its own API key. (Bedrock + SNS are added in steps 5 and 6.)
resource "aws_iam_role_policy" "read_api_key" {
  name = "read-api-key"
  role = aws_iam_role.server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:GetParameter"
      Resource = aws_ssm_parameter.api_key.arn
    }]
  })
}

# Lets you open a shell from the AWS console (Session Manager) as a backup to SSH.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "server" {
  name = "apl-temp-server"
  role = aws_iam_role.server.name
}

# ---------- The server ----------

resource "aws_key_pair" "ssh" {
  key_name   = "apl-temp"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "aws_instance" "web" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  key_name               = aws_key_pair.ssh.key_name
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.server.name

  # First boot: install Ansible, download the repo, run the playbook (same idea as level 3).
  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    repo_url = var.repo_url
    region   = var.region
  })
  user_data_replace_on_change = true

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
    encrypted   = true
  }

  # IMDSv2: safer way for the server to get its role credentials.
  metadata_options {
    http_tokens = "required"
  }

  tags = { Name = "apl-temp-web" }

  # A newer AMI appears every few weeks - don't rebuild the server just for that.
  lifecycle {
    ignore_changes = [ami]
  }
}

# Fixed public IP: the ESP32 has this address built in, so it must never change.
resource "aws_eip" "web" {
  instance = aws_instance.web.id
  domain   = "vpc"
  tags     = { Name = "apl-temp-web" }
}
