# docker-bake.hcl

# Define a variable for the image name to keep it consistent.
variable "IMAGE_NAME" {
  default = "nether-bridge"
}

# Define a common group of settings that all targets can inherit.
# This avoids repetition and includes your cache settings.
target "build-defaults" {
  dockerfile = "Dockerfile"
  context    = "."
  cache-from = ["type=registry,ref=ghcr.io/meek2100/mcbd-proxy-cache:main"]
  cache-to   = ["type=registry,ref=ghcr.io/meek2100/mcbd-proxy-cache:main,mode=max"]
}

# Define the "default" group. Running `docker bake` with no arguments
# will build all targets listed here. This is perfect for your test setup.
group "default" {
  targets = ["nether-bridge", "nb-tester"]
}

# Define the target for your final production image.
# It inherits the defaults and specifies the final stage and image tag.
target "nether-bridge" {
  inherits = ["build-defaults"]
  target   = "final"
  tags     = ["${IMAGE_NAME}:local"]
}

# Define the target for your CI test runner image.
# It inherits the defaults and specifies the testing stage and image tag.
target "nb-tester" {
  inherits = ["build-defaults"]
  target   = "testing"
  tags     = ["nb-tester:local"]
}