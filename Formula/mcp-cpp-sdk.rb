class McpCppSdk < Formula
  desc "Model Context Protocol C++ SDK"
  homepage "https://github.com/yurirocha15/mcp-cpp-sdk"
  url "https://github.com/yurirocha15/mcp-cpp-sdk/releases/download/v0.3.0/mcp-cpp-sdk-0.3.0.tar.gz"
  sha256 "31ad6f5f63eb1b9101f0ecaac54db10021ecfec0b2fe85740a8172e0cc62dd20"
  license all_of: ["Apache-2.0", "BSL-1.0"]

  bottle do
    root_url "https://ghcr.io/v2/yurirocha15/mcp-cpp-sdk"
    sha256 cellar: :any, arm64_sequoia: "9e49321d2aa1f29ddcaa82714faf6cc292cf65413035d323aacb2716b2e0dfe7"
    sha256 cellar: :any, sequoia:       "aca31bfa6e21b4b176f94088cc8222da3a70f75469dbe2350ef7dcc8ee335113"
    sha256 cellar: :any, arm64_linux:   "6e0ade67e8f5d68cd5242ee3030745f27f94a79be12399b4fb011b8dcf7177d6"
    sha256 cellar: :any, x86_64_linux:  "eec8c660b4d4c68b6e48911f6fce442256dc7d384162b4d9a776c38c66b8de3c"
  end

  depends_on "cmake" => :build
  depends_on "googletest" => :build
  depends_on "ninja" => :build
  depends_on "boost"
  depends_on "nlohmann-json"
  depends_on "openssl@3"

  def install
    args = std_cmake_args + %w[
      -DMCP_CPP_SDK_BUILD_SHARED=ON
      -DMCP_CPP_SDK_BUILD_STATIC=ON
      -DBUILD_TESTING=ON
      -DBUILD_EXAMPLES=OFF
      -DBUILD_DOCS=OFF
    ]
    system "cmake", "-S", ".", "-B", "build", "-G", "Ninja", *args
    system "cmake", "--build", "build"
    system "ctest", "--test-dir", "build", "--output-on-failure"
    system "cmake", "--install", "build"
  end

  test do
    (testpath/"CMakeLists.txt").write <<~CMAKE
      cmake_minimum_required(VERSION 3.20)
      project(mcp_cpp_sdk_consumer LANGUAGES CXX)
      find_package(mcp-cpp-sdk CONFIG REQUIRED COMPONENTS shared static)
      add_executable(shared-consumer main.cpp)
      target_link_libraries(shared-consumer PRIVATE mcp::sdk_shared)
      add_executable(static-consumer main.cpp)
      target_link_libraries(static-consumer PRIVATE mcp::sdk_static)
    CMAKE
    (testpath/"main.cpp").write <<~CPP
      #include <mcp/mcp.hpp>
      int main() { return mcp::version().empty() ? 1 : 0; }
    CPP
    system "cmake", "-S", ".", "-B", "build", "-DCMAKE_PREFIX_PATH=#{prefix}"
    system "cmake", "--build", "build"
    system "./build/shared-consumer"
    system "./build/static-consumer"
  end
end
