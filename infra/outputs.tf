output "public_ip" {
  value = aws_eip.web.public_ip
}

output "url" {
  value = "http://${aws_eip.web.public_ip}"
}

output "esp32_server_url" {
  description = "Put this in temp-sensor/include/secrets.h as SERVER_URL"
  value       = "http://${aws_eip.web.public_ip}/api/readings"
}

output "ssh" {
  value = "ssh -i ~/.ssh/apl_temp_ed25519 ec2-user@${aws_eip.web.public_ip}"
}
