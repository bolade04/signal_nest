# Staging window (docs/operations/staging-window.md): the root ANDs the operator's
# enable_nat_gateway with staging_window_active, so a closed window drives this module with
# enable_nat_gateway = false. This proves that state plans NO NAT gateway, NO Elastic IP
# (the chargeable public IPv4 address is released) and NO private default route, while the
# VPC, subnets and route tables persist. Offline: mocked provider.
mock_provider "aws" {}

variables {
  name_prefix        = "signalnest-staging"
  vpc_cidr           = "10.20.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
  subnet_newbits     = 4
}

run "nat_enabled_plans_gateway_eip_and_private_default_route" {
  command = plan

  variables {
    enable_nat_gateway = true
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 1 && length(aws_eip.nat) == 1 && length(aws_route.private_default) == 1
    error_message = "with NAT enabled the gateway, its EIP and the private default route must be planned"
  }
}

run "nat_disabled_releases_the_address_and_keeps_the_network" {
  command = plan

  variables {
    enable_nat_gateway = false
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 0 && length(aws_eip.nat) == 0 && length(aws_route.private_default) == 0
    error_message = "with NAT disabled no gateway, EIP or private default route may be planned (the public IPv4 charge stops)"
  }

  assert {
    condition     = length(aws_subnet.private) == 2 && length(aws_subnet.public) == 2 && aws_route_table.private.vpc_id != null
    error_message = "subnets and route tables persist across windows"
  }
}
