/* Static aarch64/arm/x86_64 helper: bring the loopback interface up.
 *
 * Why a compiled binary instead of shell: the converted images carry neither
 * `ip` nor `ifconfig`, and many do not even have python3. Bringing an interface
 * up needs an ioctl, so the only portable option is a tiny static ELF placed on
 * the payload disk. It is compiled once per host architecture and cached.
 *
 * No network namespace is configured for these VMs (Firecracker boots without a
 * NIC), so loopback is all a guest that binds to 127.0.0.1 can ever use.
 */
#include <net/if.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void) {
    struct ifreq req;
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }

    memset(&req, 0, sizeof(req));
    strncpy(req.ifr_name, "lo", IFNAMSIZ - 1);
    if (ioctl(fd, SIOCGIFFLAGS, &req) < 0) { perror("SIOCGIFFLAGS"); return 2; }
    req.ifr_flags |= (short)(IFF_UP | IFF_RUNNING);
    if (ioctl(fd, SIOCSIFFLAGS, &req) < 0) { perror("SIOCSIFFLAGS"); return 3; }

    memset(&req, 0, sizeof(req));
    strncpy(req.ifr_name, "lo", IFNAMSIZ - 1);
    if (ioctl(fd, SIOCGIFFLAGS, &req) < 0) { perror("verify"); return 4; }
    if (!(req.ifr_flags & IFF_UP)) { fputs("lo still down\n", stderr); return 5; }

    /* Marker the host recognises; keeps success/failure observable on serial. */
    puts("BWX_LO_READY");
    close(fd);
    return 0;
}
