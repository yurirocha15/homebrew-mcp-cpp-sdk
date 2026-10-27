class McpCppSdk < Formula
  desc "Model Context Protocol C++ SDK"
  homepage "https://github.com/yurirocha15/mcp-cpp-sdk"
  url "https://github.com/yurirocha15/mcp-cpp-sdk/releases/download/v0.2.0/mcp-cpp-sdk-0.2.0.tar.gz"
  sha256 "f2a7b11e3fbb6ed6e67b649950a4d6d8a766cf71b59fe5bc795c1cdcd38868b4"
  license "Apache-2.0"

  bottle do
    root_url "https://ghcr.io/v2/yurirocha15/mcp-cpp-sdk"
    sha256 cellar: :any, arm64_sequoia: "38cb64ee98d4d57f94622c58297b8e502f61f1e2adefd6cc99129323de58813a"
    sha256 cellar: :any, sequoia:       "3bfb9fb9008159b88cce87cbe8f405a98b15c33b04527473feb8965796699f25"
    sha256 cellar: :any, arm64_linux:   "56446517c84795ed8674465aba67a8fc6e465f1f787b763babb802fc7e29f679"
    sha256 cellar: :any, x86_64_linux:  "4420522d761cc9c00fd7dbc27d00672452f0ac27aa02e564622f50f6074fd060"
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
