# Inert negative-control fixture for the IaC gate. No provider, no backend, never applied.
# An operator-independent, intentionally failing check: unrestricted SSH ingress (AWS-0107).
resource "aws_security_group_rule" "negative_control_open_ssh" {
  type              = "ingress"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = "sg-00000000000000000"
}
