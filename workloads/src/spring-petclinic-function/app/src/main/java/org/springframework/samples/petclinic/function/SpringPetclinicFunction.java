package org.springframework.samples.petclinic.function;

import rawhttp.core.RawHttp;
import rawhttp.core.RawHttpRequest;
import rawhttp.core.errors.InvalidHttpHeader;
import rawhttp.core.errors.InvalidHttpRequest;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.channels.Channel;
import java.nio.channels.Channels;
import java.nio.channels.ReadableByteChannel;
import java.nio.channels.WritableByteChannel;
import java.nio.charset.StandardCharsets;
import java.time.LocalDate;

import org.springframework.samples.petclinic.owner.Owner;
import org.springframework.samples.petclinic.owner.Pet;
import org.springframework.samples.petclinic.owner.PetType;
import org.springframework.samples.petclinic.owner.Visit;

public final class SpringPetclinicFunction {
    private SpringPetclinicFunction() {
    }

    public static void main(String[] args) throws Exception {
        if (Boolean.getBoolean("FAAS_COMPATIBLE")) {
            Channel channel = System.inheritedChannel();
            try (InputStream in = Channels.newInputStream((ReadableByteChannel) channel);
                 OutputStream out = Channels.newOutputStream((WritableByteChannel) channel)) {
                run(in, out);
            }
        } else {
            run(System.in, System.out);
        }
    }

    static void run(InputStream inputStream, OutputStream outputStream) throws IOException {
        RawHttp rawHttp = new RawHttp();
        PetclinicScenario scenario = createPetclinicScenario();
        while (true) {
            try {
                RawHttpRequest request = rawHttp.parseRequest(inputStream);
                writeResponse(outputStream, route(request, scenario));
            } catch (InvalidHttpRequest | InvalidHttpHeader e) {
                break;
            } catch (Exception e) {
                writeResponse(outputStream, 500, "text/plain", "spring-petclinic-function failed: " + e.getMessage() + "\n");
            }
        }
    }

    private static Response route(RawHttpRequest request, PetclinicScenario scenario) {
        String path = request.getUri().getPath();
        if ("GET".equals(request.getMethod()) && ("/petclinic/smoke-test".equals(path) || "/petclinic/smoke-test/".equals(path))) {
            return new Response(200, "text/plain", scenario.run());
        }
        if ("GET".equals(request.getMethod()) && "/".equals(path)) {
            return new Response(200, "text/plain", "spring-petclinic-function\n");
        }
        return new Response(404, "text/plain", "No route for " + request.getMethod() + " " + path + "\n");
    }

    private static PetclinicScenario createPetclinicScenario() {
        Owner owner = createOwner(1001, "F1001", "L1001", "A1001", "C1001", "5550001001");
        PetType cat = petType(1, "cat");
        Pet pet = createPet("Pet2001", LocalDate.of(2010, 10, 5), cat);
        owner.addPet(pet);
        pet.setId(2001);

        Pet readPet = owner.getPet(2001);
        Visit visit = new Visit();
        visit.setId(3001);
        visit.setDate(LocalDate.of(2021, 2, 23));
        visit.setDescription("Broken leg");
        owner.addVisit(readPet.getId(), visit);

        PetType dog = petType(2, "dog");
        return new PetclinicScenario(owner, readPet, visit, cat, dog);
    }

    private static Owner createOwner(int id, String firstName, String lastName, String address, String city, String telephone) {
        Owner owner = new Owner();
        owner.setId(id);
        owner.setFirstName(firstName);
        owner.setLastName(lastName);
        owner.setAddress(address);
        owner.setCity(city);
        owner.setTelephone(telephone);
        return owner;
    }

    private static Pet createPet(String name, LocalDate birthDate, PetType type) {
        Pet pet = new Pet();
        pet.setName(name);
        pet.setBirthDate(birthDate);
        pet.setType(type);
        return pet;
    }

    private static PetType petType(int id, String name) {
        PetType petType = new PetType();
        petType.setId(id);
        petType.setName(name);
        return petType;
    }

    private static String renderOwner(Owner owner, Pet pet, Visit visit) {
        return "spring-petclinic smoke-test\n"
            + "owner=" + owner.getId() + " " + owner.getFirstName() + " " + owner.getLastName() + "\n"
            + "city=" + owner.getCity() + "\n"
            + "pet=" + pet.getId() + " " + pet.getName() + " " + pet.getType().getName() + "\n"
            + "visit=" + visit.getDate() + " " + visit.getDescription() + "\n"
            + "visit_link=/owners/" + owner.getId() + "/pets/" + pet.getId() + "/visits/new\n"
            + "pets_count=" + owner.getPets().size() + "\n"
            + "visits_count=" + pet.getVisits().size() + "\n";
    }

    private static void writeResponse(OutputStream outputStream, Response response) throws IOException {
        writeResponse(outputStream, response.statusCode(), response.contentType(), response.body());
    }

    private static void writeResponse(OutputStream outputStream, int statusCode, String contentType, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        String reason = statusCode == 200 ? "OK" : statusCode == 404 ? "Not Found" : "Internal Server Error";
        String headers = "HTTP/1.1 " + statusCode + " " + reason + "\r\n"
            + "Content-Type: " + contentType + "\r\n"
            + "Content-Length: " + bytes.length + "\r\n"
            + "Connection: keep-alive\r\n\r\n";
        outputStream.write(headers.getBytes(StandardCharsets.US_ASCII));
        outputStream.write(bytes);
        outputStream.flush();
    }

    private record Response(int statusCode, String contentType, String body) {
    }

    private static final class PetclinicScenario {
        private final Owner owner;
        private final Pet pet;
        private final Visit visit;
        private final PetType cat;
        private final PetType dog;
        private boolean updated;

        private PetclinicScenario(Owner owner, Pet pet, Visit visit, PetType cat, PetType dog) {
            this.owner = owner;
            this.pet = pet;
            this.visit = visit;
            this.cat = cat;
            this.dog = dog;
        }

        private String run() {
            Pet readPet = owner.getPet(pet.getId());
            updated = !updated;
            if (updated) {
                updateOwner("F2001", "L2001", "A2001", "C2001", "5550002001");
                readPet.setName("Pet2002");
                readPet.setType(dog);
            } else {
                updateOwner("F1001", "L1001", "A1001", "C1001", "5550001001");
                readPet.setName("Pet2001");
                readPet.setType(cat);
            }
            return renderOwner(owner, readPet, visit);
        }

        private void updateOwner(String firstName, String lastName, String address, String city, String telephone) {
            owner.setFirstName(firstName);
            owner.setLastName(lastName);
            owner.setAddress(address);
            owner.setCity(city);
            owner.setTelephone(telephone);
        }
    }
}
