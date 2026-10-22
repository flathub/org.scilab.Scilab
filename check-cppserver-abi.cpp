// Verify that Scilab's C++17 headers agree with the compiled CppServer library.
#include <server/http/http_server.h>

#include <iostream>
#include <memory>

int main()
{
    auto service = std::make_shared<CppServer::Asio::Service>();
    auto server = std::make_shared<CppServer::HTTP::HTTPServer>(service, "127.0.0.1", 0);
    CppServer::HTTP::HTTPSession session(server);

    // The session constructor in libcppserver stores the server's cache address.
    // A different Asio endpoint layout shifts that address inside HTTPServer.
    if (&session.cache() != &server->cache())
    {
        std::cerr << "CppServer ABI mismatch: HTTP session refers to the wrong cache\n";
        return 1;
    }
    std::cout << "CppServer C++17 ABI check passed\n";
}
